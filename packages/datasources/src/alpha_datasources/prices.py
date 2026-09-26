"""按时间点查 token 美元价格（给历史事件计价用）。

本层只负责"从数据源取到某个时间桶的价格"，并通过 `PriceStore` 缓存已收盘的时间桶；
"稳定币按 1、同一笔交易里的 swap 隐含价格优先"这类取价策略属于上层定价器（钱包分析 M3），不在这里。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import requests
from alpha_core.errors import DataSourceUnavailableError
from alpha_core.metering import CallMeter
from alpha_core.ports import PriceConfidence, PriceGranularity, PricePoint, PriceStore
from alpha_core.types import Chain

from ._http import Throttle, metered_get
from .geckoterminal import GeckoTerminalClient, TokenPool

logger = logging.getLogger(__name__)

# 可信的基础资产：长尾 token 只通过和它们组成的池子取价（借鉴 DefiLlama 的 coreAssets 思路）。
CORE_ASSETS: dict[Chain, frozenset[str]] = {
    Chain.BSC: frozenset(
        {
            "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
            "0x55d398326f99059ff775485246999027b3197955",  # USDT
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
            "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
            "0xc5f0f7b66764f6ec8c8dff7ba683102295e16409",  # FDUSD
            "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",  # BTCB
            "0x2170ed0880ac9a755fd29b2688956bd959f933f8",  # ETH（Binance-Peg）
        }
    )
}

# 池子 TVL 到可信度的映射阈值（美元）。
HIGH_CONFIDENCE_RESERVE_USD = 1_000_000
MEDIUM_CONFIDENCE_RESERVE_USD = 100_000

_BUCKET = {PriceGranularity.DAY: timedelta(days=1), PriceGranularity.HOUR: timedelta(hours=1)}


def bucket_start(at: datetime, granularity: PriceGranularity) -> datetime:
    """时间点所在时间桶的起点（UTC）。"""
    at = at.astimezone(UTC)
    if granularity == PriceGranularity.DAY:
        return at.replace(hour=0, minute=0, second=0, microsecond=0)
    return at.replace(minute=0, second=0, microsecond=0)


class TokenPriceSource(Protocol):
    """价格数据源：返回某个时间桶的价格，查不到返回 None，临时故障抛 `DataSourceUnavailableError`。"""

    name: str

    def get_bucket_price(
        self, chain: Chain, token_address: str, start: datetime, granularity: PriceGranularity
    ) -> PricePoint | None: ...


class GeckoTerminalPriceSource:
    """用 GeckoTerminal 的池子 K 线取价：选和基础资产组成、TVL 最大的池子，取对应时间桶的收盘价。"""

    name = "geckoterminal"

    def __init__(self, client: GeckoTerminalClient) -> None:
        self._client = client
        self._pool_cache: dict[tuple[Chain, str], TokenPool | None] = {}

    def _pick_pool(self, chain: Chain, token: str) -> TokenPool | None:
        key = (chain, token)
        if key not in self._pool_cache:
            core = CORE_ASSETS.get(chain, frozenset())
            candidates = [
                p
                for p in self._client.get_token_pools(token)
                if token in (p.base_token, p.quote_token)
                and (p.quote_token if p.base_token == token else p.base_token) in core
            ]
            self._pool_cache[key] = max(candidates, key=lambda p: p.reserve_usd or 0.0, default=None)
        return self._pool_cache[key]

    def get_bucket_price(
        self, chain: Chain, token_address: str, start: datetime, granularity: PriceGranularity
    ) -> PricePoint | None:
        token = token_address.lower()
        pool = self._pick_pool(chain, token)
        if pool is None:
            return None
        end = start + _BUCKET[granularity]
        candles = self._client.get_usd_ohlcv_before(
            pool.pool_address,
            token,
            timeframe="day" if granularity == PriceGranularity.DAY else "hour",
            before_timestamp=int(end.timestamp()),
            limit=1,
        )
        if not candles:
            return None
        candle = candles[-1]
        reserve = pool.reserve_usd or 0.0
        if candle.start != start:
            # 该时间桶没有成交，拿到的是更早一根 K 线的收盘价，只作参考。
            confidence = PriceConfidence.LOW
        elif reserve >= HIGH_CONFIDENCE_RESERVE_USD:
            confidence = PriceConfidence.HIGH
        elif reserve >= MEDIUM_CONFIDENCE_RESERVE_USD:
            confidence = PriceConfidence.MEDIUM
        else:
            confidence = PriceConfidence.LOW
        return PricePoint(
            chain=chain.value,
            token_address=token,
            granularity=granularity,
            bucket_start=start,
            price_usd=Decimal(str(candle.close_usd)),
            source=self.name,
            source_ref=pool.pool_address,
            confidence=confidence,
        )


class CoinGeckoPriceSource:
    """CoinGecko 按合约地址查历史价格：`/coins/{平台}/contract/{地址}/market_chart/range`。

    免费公共接口限流严（约 10 次/分钟），只作为 GeckoTerminal 查不到时的兜底。
    """

    name = "coingecko"
    BASE_URL = "https://api.coingecko.com/api/v3"
    PLATFORMS: dict[Chain, str] = {Chain.BSC: "binance-smart-chain"}

    def __init__(self, *, session: requests.Session | None = None, meter: CallMeter | None = None) -> None:
        self._session = session or requests.Session()
        self._meter = meter
        self._throttle = Throttle(6.0)

    def get_bucket_price(
        self, chain: Chain, token_address: str, start: datetime, granularity: PriceGranularity
    ) -> PricePoint | None:
        token = token_address.lower()
        end = start + _BUCKET[granularity]
        resp = metered_get(
            self._session,
            f"{self.BASE_URL}/coins/{self.PLATFORMS[chain]}/contract/{token}/market_chart/range",
            provider=self.name,
            method="/coins/:platform/contract/:addr/market_chart/range",
            params={"vs_currency": "usd", "from": int(start.timestamp()), "to": int(end.timestamp())},
            meter=self._meter,
            throttle=self._throttle,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise DataSourceUnavailableError(f"coingecko HTTP {resp.status_code}: {resp.text[:200]}")
        prices = resp.json().get("prices") or []
        if not prices:
            return None
        _ts_ms, price = prices[-1]  # 时间桶内最后一个点，近似收盘价
        return PricePoint(
            chain=chain.value,
            token_address=token,
            granularity=granularity,
            bucket_start=start,
            price_usd=Decimal(str(price)),
            source=self.name,
            source_ref=None,
            confidence=PriceConfidence.MEDIUM,
        )


class CachingPriceFetcher:
    """按"缓存 → 各数据源依次尝试 → 写缓存"取价；只缓存已经收盘的时间桶。"""

    def __init__(
        self,
        sources: list[TokenPriceSource],
        *,
        store: PriceStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sources = sources
        self._store = store
        self._clock = clock

    def get_price_at(
        self, chain: Chain, token_address: str, at: datetime, granularity: PriceGranularity = PriceGranularity.DAY
    ) -> PricePoint | None:
        """返回 `at` 所在时间桶的价格；所有来源都查不到返回 None。

        @raises DataSourceUnavailableError 所有来源都是临时故障
        """
        token = token_address.lower()
        start = bucket_start(at, granularity)
        closed = start + _BUCKET[granularity] <= self._clock()
        if closed and self._store is not None:
            cached = self._store.get(chain.value, token, granularity, start)
            if cached is not None:
                return cached
        errors: list[str] = []
        answered = False
        for source in self._sources:
            try:
                point = source.get_bucket_price(chain, token, start, granularity)
            except DataSourceUnavailableError as exc:
                errors.append(f"{source.name}: {exc}")
                logger.warning("价格源 %s 查询失败: %s", source.name, exc)
                continue
            answered = True
            if point is not None:
                if closed and self._store is not None:
                    self._store.put(point)
                return point
        if not answered and errors:
            raise DataSourceUnavailableError("价格源均不可用: " + "; ".join(errors))
        return None
