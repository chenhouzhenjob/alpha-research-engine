"""GeckoTerminal Public API 适配器。

数据用途边界（对应 pool-discovery-metrics-v1.md 第 0 节）：
只作为收益/风险信号来源（TVL/24h volume/OHLCV），不作为池子存在性权威——
池子身份始终以 Factory 链上事件为准，本适配器只按地址查询已发现的候选池。

已知局限（写入 apps/lp-backtest/README.md 时需要引用）：
GeckoTerminal 公开 API 对 TVL/24h volume 只暴露"当前值"，不提供历史时间序列；
真正意义上的"逐日 TVL/volume 历史"只能靠本系统每天调用一次、自己积累快照，
无法一次性回填过去 30 天的 TVL/volume（只有 OHLCV 收盘价可以一次性回填历史）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

import requests
from alpha_core.errors import DataSourceUnavailableError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .base import MarketDataSource, MinuteOhlcvPoint, OhlcvPoint, PoolMarketSnapshot

logger = logging.getLogger(__name__)

BASE_URL = "https://api.geckoterminal.com/api/v2"
# 官方文档说的是 30 次/分钟，但实测按 30 卡节流仍会偶发 429（可能是官方限流窗口的实现方式更严格，
# 或者和其他并发调用方共享同一限流桶），打个安全折扣，避免一直靠重试硬扛。
RATE_LIMIT_CALLS_PER_MINUTE = 20
_MIN_INTERVAL_SECONDS = 60.0 / RATE_LIMIT_CALLS_PER_MINUTE

# 官方文档未给出 /pools/multi 单次最大地址数，取一个与限流数量级一致的保守值，
# 遇到 4xx 报"参数过多"时再下调（本期未观察到该报错）。
MAX_ADDRESSES_PER_MULTI_CALL = 30

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class _RetryableHttpError(Exception):
    """标记可重试的 HTTP 状态码（限流/网关类错误），触发 tenacity 重试。"""


def _to_float(value) -> float | None:
    return float(value) if value is not None else None


def _parse_iso8601(value) -> datetime | None:
    """解析 GeckoTerminal 返回的 `pool_created_at`（如 "2025-11-14T07:04:27Z"）。"""
    if not value:
        return None
    return datetime.fromisoformat(value)


class GeckoTerminalClient(MarketDataSource):
    """GeckoTerminal 客户端，内置限流节流与重试。"""

    def __init__(self, network: str = "bsc", session: requests.Session | None = None) -> None:
        self._network = network
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._last_call_at: float = 0.0

    def _throttle(self) -> None:
        """确保相邻请求间隔不低于限流要求，避免触发 429。"""
        with self._lock:
            wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_call_at)
            if wait > 0:
                time.sleep(wait)
            self._last_call_at = time.monotonic()

    @retry(
        reraise=True,
        stop=stop_after_attempt(8),
        wait=wait_exponential(multiplier=2, min=2, max=90),
        retry=retry_if_exception_type((_RetryableHttpError, requests.RequestException)),
    )
    def _get(self, path: str, *, params: dict | None = None) -> dict:
        self._throttle()
        resp = self._session.get(f"{BASE_URL}{path}", params=params, timeout=15)
        if resp.status_code in _RETRYABLE_STATUS_CODES:
            raise _RetryableHttpError(f"GET {path} -> {resp.status_code}")
        if resp.status_code == 404:
            raise DataSourceUnavailableError(f"GeckoTerminal 404: {path}")
        resp.raise_for_status()
        return resp.json()

    def get_pool_snapshots(self, pool_addresses: list[str]) -> dict[str, PoolMarketSnapshot]:
        snapshots: dict[str, PoolMarketSnapshot] = {}
        for i in range(0, len(pool_addresses), MAX_ADDRESSES_PER_MULTI_CALL):
            batch = pool_addresses[i : i + MAX_ADDRESSES_PER_MULTI_CALL]
            try:
                payload = self._get(
                    f"/networks/{self._network}/pools/multi/{','.join(batch)}"
                )
            except DataSourceUnavailableError:
                logger.warning("GeckoTerminal 批量查询整批未命中: %s", batch)
                continue
            fetched_at = datetime.now(UTC)
            for entry in payload.get("data", []):
                attrs = entry.get("attributes", {})
                address = attrs.get("address", "").lower()
                if not address:
                    continue
                snapshots[address] = PoolMarketSnapshot(
                    pool_address=address,
                    tvl_usd=_to_float(attrs.get("reserve_in_usd")),
                    volume_24h_usd=_to_float(attrs.get("volume_usd", {}).get("h24")),
                    close_price=_to_float(attrs.get("base_token_price_quote_token")),
                    created_at=_parse_iso8601(attrs.get("pool_created_at")),
                    name=attrs.get("name"),
                    fetched_at=fetched_at,
                )
        return snapshots

    def get_daily_ohlcv(self, pool_address: str, *, days: int) -> list[OhlcvPoint]:
        """按日拉取该池子的历史 K 线。

        `currency=token`（不是 `usd`）：返回 base/quote 汇率（等价于 `get_pool_snapshots` 里
        `base_token_price_quote_token` 的历史版本），而不是 base token 的 USD 价格——
        这里必须和 `get_pool_snapshots` 用同一个量纲，否则历史序列和"今天"这一条会因为单位不同
        而拼出一条假的价格跳变（实测 CAKE/WBNB 曾用 currency=usd 拉出 ~1.4 的历史价，
        而当前快照走的是 base/quote 汇率 ~0.0024，两者相差近 600 倍，是这次修复的直接起因）。
        """
        try:
            payload = self._get(
                f"/networks/{self._network}/pools/{pool_address}/ohlcv/day",
                params={"aggregate": 1, "limit": days, "currency": "token"},
            )
        except DataSourceUnavailableError:
            logger.warning("GeckoTerminal 该池子无 OHLCV 数据: %s", pool_address)
            return []
        rows = payload.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        points = [
            OhlcvPoint(
                day=datetime.fromtimestamp(row[0], tz=UTC).date(),
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
            )
            for row in rows
        ]
        return sorted(points, key=lambda p: p.day)

    def get_minute_ohlcv(self, pool_address: str, *, limit: int = 10) -> list[MinuteOhlcvPoint]:
        """按分钟拉取该池子最近 `limit` 根 1 分钟 K 线，币种口径同 `get_daily_ohlcv`
        （`currency=token`，理由一致：跟 `get_pool_snapshots` 用同一个量纲，避免拼出假的价格跳变）。

        用这个替代自己订阅 Swap 事件 + 现场聚合（`lp_backtest.aggregate_candles` 的旧路径）——
        GeckoTerminal 自己的索引管线已经把这件事做了，实测延迟约 2 分钟，比自建 WSS 订阅+聚合
        简单得多，见 apps/live-signal 的部署记录。`swap_events`/WebSocket 订阅仍然保留，
        但只用来采集逐笔明细供未来风控信号（如大户集中度）用，不再是 K 线的数据来源。
        """
        try:
            payload = self._get(
                f"/networks/{self._network}/pools/{pool_address}/ohlcv/minute",
                params={"aggregate": 1, "limit": limit, "currency": "token"},
            )
        except DataSourceUnavailableError:
            logger.warning("GeckoTerminal 该池子无分钟级 OHLCV 数据: %s", pool_address)
            return []
        rows = payload.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        points = [
            MinuteOhlcvPoint(
                ts_event=datetime.fromtimestamp(row[0], tz=UTC),
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
            )
            for row in rows
        ]
        return sorted(points, key=lambda p: p.ts_event)
