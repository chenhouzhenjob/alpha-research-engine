"""数据源 HTTP 基础设施、ABI 多来源解析、历史价格。响应样本按 2026-09-26 实测的真实格式构造。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from alpha_core.errors import DataSourceUnavailableError, SourceRateLimitedError
from alpha_core.metering import CallStatus, InMemoryCallMeter
from alpha_core.ports import AbiKeyType, AbiStatus, PriceConfidence, PriceGranularity, PricePoint
from alpha_core.types import Chain
from alpha_datasources._http import BreakerState, CircuitBreaker, metered_get
from alpha_datasources.abi_sources import (
    AbiResolver,
    FourByteSource,
    LookupKind,
    LookupResult,
    OpenchainSource,
    SourcifySource,
)
from alpha_datasources.geckoterminal import TokenPool, UsdCandle
from alpha_datasources.prices import CachingPriceFetcher, GeckoTerminalPriceSource, bucket_start

NPM = "0x46a15b0b27311cedf172ab29e4f4766fbe7f4364"
TRANSFER_SIG = "0xa9059cbb"
USDT = "0x55d398326f99059ff775485246999027b3197955"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
CAKE = "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82"


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_metered_get_retries_5xx_then_returns_and_meters():
    meter = InMemoryCallMeter(app="t")
    s = FakeSession([Resp(502), Resp(200, {"ok": 1})])
    r = metered_get(s, "u", provider="p", method="/m", meter=meter, sleep=lambda _x: None)
    assert r.status_code == 200
    totals = {k.status: v.call_count for k, v in meter.snapshot().items()}
    assert totals == {CallStatus.ERROR: 1, CallStatus.OK: 1}


def test_metered_get_429_exhausts_to_rate_limited_error():
    s = FakeSession([Resp(429)] * 3)
    with pytest.raises(SourceRateLimitedError):
        metered_get(s, "u", provider="p", method="/m", sleep=lambda _x: None)


def test_metered_get_returns_404_for_caller_to_interpret():
    assert metered_get(FakeSession([Resp(404)]), "u", provider="p", method="/m").status_code == 404


def test_circuit_breaker_opens_then_half_opens():
    now = [0.0]
    b = CircuitBreaker(threshold=2, cooldown=10, clock=lambda: now[0])
    b.on_failure()
    assert b.state == BreakerState.CLOSED
    b.on_failure()
    assert b.state == BreakerState.OPEN and not b.allow()
    now[0] = 10
    assert b.state == BreakerState.HALF_OPEN and b.allow()
    b.on_failure()  # 试探失败，重新打开
    assert b.state == BreakerState.OPEN
    now[0] = 20
    b.on_success()
    assert b.state == BreakerState.CLOSED


def test_sourcify_parses_found_and_not_found():
    body = {"abi": [{"type": "event", "name": "Approval"}], "compilation": {"name": "NonfungiblePositionManager"}}
    src = SourcifySource(session=FakeSession([Resp(200, body), Resp(404, {"match": None})]))
    found = src.lookup(Chain.BSC, AbiKeyType.ADDRESS, NPM)
    assert (found.kind, found.name) == (LookupKind.FOUND, "NonfungiblePositionManager")
    assert src.lookup(Chain.BSC, AbiKeyType.ADDRESS, "0x" + "12" * 20).kind == LookupKind.NOT_FOUND


def test_openchain_prefers_unfiltered_verified():
    body = {
        "ok": True,
        "result": {
            "function": {
                TRANSFER_SIG: [
                    {"name": "junk_123(bytes)", "filtered": True, "hasVerifiedContract": False},
                    {"name": "transfer(address,uint256)", "filtered": False, "hasVerifiedContract": True},
                ]
            }
        },
    }
    res = OpenchainSource(session=FakeSession([Resp(200, body)])).lookup(Chain.BSC, AbiKeyType.FUNCTION, TRANSFER_SIG)
    assert res.name == "transfer(address,uint256)"
    assert res.abi == ["transfer(address,uint256)", "junk_123(bytes)"]


def test_fourbyte_prefers_oldest_signature():
    body = {
        "results": [
            {"id": 1111734, "text_signature": "workMyDirefulOwner(uint256,uint256)"},
            {"id": 31780, "text_signature": "transfer(address,uint256)"},
        ]
    }
    res = FourByteSource(session=FakeSession([Resp(200, body)])).lookup(Chain.BSC, AbiKeyType.FUNCTION, TRANSFER_SIG)
    assert res.name == "transfer(address,uint256)"


class MemAbiStore:
    def __init__(self):
        self.data = {}

    def get(self, chain, key_type, key):
        return self.data.get((chain, key_type, key))

    def put(self, entry):
        self.data[(entry.chain, entry.key_type, entry.key)] = entry


class StubSource:
    def __init__(self, name, results, types=(AbiKeyType.FUNCTION, AbiKeyType.EVENT)):
        self.name = name
        self.results = list(results)
        self.types = types
        self.calls = 0

    def supports(self, key_type):
        return key_type in self.types

    def lookup(self, chain, key_type, key):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _resolver(sources, store, now):
    return AbiResolver(sources, store=store, clock=lambda: now[0], breaker_factory=lambda: CircuitBreaker(threshold=1))


def test_resolver_falls_through_and_caches_success_under_signature_chain():
    now = [datetime(2026, 9, 26, tzinfo=UTC)]
    store = MemAbiStore()
    a = StubSource("openchain", [LookupResult(LookupKind.NOT_FOUND)])
    b = StubSource(
        "4byte", [LookupResult(LookupKind.FOUND, "transfer(address,uint256)", ["transfer(address,uint256)"])]
    )
    entry = _resolver([a, b], store, now).resolve(Chain.BSC, AbiKeyType.FUNCTION, "0xA9059CBB")
    assert (entry.status, entry.source, entry.chain, entry.key) == (AbiStatus.SUCCESS, "4byte", "*", TRANSFER_SIG)
    assert store.get("*", AbiKeyType.FUNCTION, TRANSFER_SIG) == entry


def test_resolver_negative_cache_blocks_requery_until_expiry():
    now = [datetime(2026, 9, 26, tzinfo=UTC)]
    store = MemAbiStore()
    src = StubSource("sourcify", [LookupResult(LookupKind.NOT_FOUND)] * 2, types=(AbiKeyType.ADDRESS,))
    r = _resolver([src], store, now)
    first = r.resolve(Chain.BSC, AbiKeyType.ADDRESS, NPM)
    assert first.status == AbiStatus.NOT_FOUND and first.retry_after == now[0] + timedelta(days=7)
    r.resolve(Chain.BSC, AbiKeyType.ADDRESS, NPM)
    assert src.calls == 1  # 负缓存命中
    now[0] += timedelta(days=8)
    r.resolve(Chain.BSC, AbiKeyType.ADDRESS, NPM)
    assert src.calls == 2  # 过期后重查


def test_resolver_all_transient_raises_and_does_not_cache():
    now = [datetime(2026, 9, 26, tzinfo=UTC)]
    store = MemAbiStore()
    src = StubSource("openchain", [DataSourceUnavailableError("down"), DataSourceUnavailableError("down")])
    r = _resolver([src], store, now)
    with pytest.raises(DataSourceUnavailableError):
        r.resolve(Chain.BSC, AbiKeyType.EVENT, "0xdd")
    assert store.data == {}
    with pytest.raises(DataSourceUnavailableError, match="熔断"):  # threshold=1，已熔断，不再请求
        r.resolve(Chain.BSC, AbiKeyType.EVENT, "0xdd")
    assert src.calls == 1


def test_resolver_invalid_is_cached_as_invalid():
    now = [datetime(2026, 9, 26, tzinfo=UTC)]
    src = StubSource("openchain", [LookupResult(LookupKind.INVALID)])
    entry = _resolver([src], MemAbiStore(), now).resolve(Chain.BSC, AbiKeyType.EVENT, "0xdd")
    assert entry.status == AbiStatus.INVALID and entry.retry_after == now[0] + timedelta(days=30)


class FakeGecko:
    def __init__(self, pools, candles):
        self.pools = pools
        self.candles = candles
        self.ohlcv_calls = []

    def get_token_pools(self, token):
        return self.pools

    def get_usd_ohlcv_before(self, pool, token, *, timeframe, before_timestamp, limit):
        self.ohlcv_calls.append((pool, timeframe, before_timestamp))
        return self.candles


DAY = datetime(2026, 8, 27, tzinfo=UTC)


def test_gecko_price_picks_largest_core_pool_and_rates_confidence():
    pools = [
        TokenPool("0xjunk", CAKE, "0x" + "99" * 20, 50_000_000, "x"),  # 对手不是基础资产，排除
        TokenPool("0xsmall", CAKE, USDT, 200_000, "x"),
        TokenPool("0xbig", CAKE, WBNB, 22_000_000, "pancakeswap_v2"),
    ]
    gecko = FakeGecko(pools, [UsdCandle(DAY, 1.76)])
    point = GeckoTerminalPriceSource(gecko).get_bucket_price(Chain.BSC, CAKE, DAY, PriceGranularity.DAY)
    assert (point.source_ref, point.price_usd, point.confidence) == ("0xbig", Decimal("1.76"), PriceConfidence.HIGH)
    assert gecko.ohlcv_calls == [("0xbig", "day", int((DAY + timedelta(days=1)).timestamp()))]


def test_gecko_stale_candle_is_low_confidence():
    gecko = FakeGecko([TokenPool("0xbig", CAKE, WBNB, 22_000_000, "x")], [UsdCandle(DAY - timedelta(days=3), 1.5)])
    point = GeckoTerminalPriceSource(gecko).get_bucket_price(Chain.BSC, CAKE, DAY, PriceGranularity.DAY)
    assert point.confidence == PriceConfidence.LOW


class MemPriceStore:
    def __init__(self):
        self.data = {}

    def get(self, chain, token, granularity, start):
        return self.data.get((chain, token, granularity, start))

    def put(self, p):
        self.data[(p.chain, p.token_address, p.granularity, p.bucket_start)] = p


class StubPriceSource:
    def __init__(self, name, result):
        self.name = name
        self.result = result
        self.calls = 0

    def get_bucket_price(self, chain, token, start, granularity):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _point(start):
    return PricePoint("bsc", CAKE, PriceGranularity.DAY, start, Decimal("2"), "coingecko", None, PriceConfidence.MEDIUM)


def test_fetcher_caches_only_closed_buckets_and_falls_back():
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    store = MemPriceStore()
    down = StubPriceSource("geckoterminal", DataSourceUnavailableError("down"))
    cg_past = StubPriceSource("coingecko", _point(DAY))
    f = CachingPriceFetcher([down, cg_past], store=store, clock=lambda: now)
    assert f.get_price_at(Chain.BSC, CAKE, DAY + timedelta(hours=5)).price_usd == Decimal("2")
    f.get_price_at(Chain.BSC, CAKE, DAY + timedelta(hours=9))
    assert cg_past.calls == 1  # 第二次命中缓存

    today = bucket_start(now, PriceGranularity.DAY)
    cg_today = StubPriceSource("coingecko", _point(today))
    f2 = CachingPriceFetcher([cg_today], store=store, clock=lambda: now)
    f2.get_price_at(Chain.BSC, CAKE, now)
    f2.get_price_at(Chain.BSC, CAKE, now)
    assert cg_today.calls == 2  # 当天未收盘，不缓存


def test_fetcher_all_sources_down_raises():
    f = CachingPriceFetcher([StubPriceSource("geckoterminal", DataSourceUnavailableError("x"))])
    with pytest.raises(DataSourceUnavailableError):
        f.get_price_at(Chain.BSC, CAKE, DAY)
