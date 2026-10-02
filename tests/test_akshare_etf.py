"""AkShare ETF 解析与 etf_iopv 落湖单测（不依赖外网）。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from alpha.collection import CanonicalStore, load_etf_iopv
from alpha.integrations.providers.akshare_etf import (
    SOURCE_IOPV_REALTIME,
    SOURCE_NAV_EOD,
    AkshareEtfProvider,
    parse_nav_eod_df,
    parse_ohlcv_hist_df,
    parse_spot_iopv_df,
)
from alpha.schema import EtfIopvPoint


def test_parse_ohlcv_hist_df() -> None:
    df = pd.DataFrame(
        [
            {
                "日期": date(2024, 1, 2),
                "开盘": 1.0,
                "收盘": 1.2,
                "最高": 1.3,
                "最低": 0.9,
                "成交量": 1000,
                "成交额": 1200.0,
            },
            {
                "日期": date(2024, 1, 3),
                "开盘": 1.2,
                "收盘": 1.1,
                "最高": 1.25,
                "最低": 1.05,
                "成交量": 800,
                "成交额": 900.0,
            },
        ]
    )
    start = int(pd.Timestamp("2024-01-02", tz="UTC").timestamp() * 1000)
    end = int(pd.Timestamp("2024-01-04", tz="UTC").timestamp() * 1000)
    bars = parse_ohlcv_hist_df(
        df,
        symbol_raw="513300",
        tf="1d",
        venue="akshare",
        market_type="etf",
        start_ms=start,
        end_ms=end,
    )
    assert len(bars) == 2
    assert bars[0].close == 1.2
    assert bars[0].instrument_id == "akshare:etf:513300"


def test_parse_nav_eod_with_close_join() -> None:
    nav = pd.DataFrame(
        [
            {"净值日期": date(2024, 1, 2), "单位净值": 1.05},
            {"净值日期": date(2024, 1, 3), "单位净值": 0.0},  # 丢弃
        ]
    )
    start = int(pd.Timestamp("2024-01-02", tz="UTC").timestamp() * 1000)
    end = int(pd.Timestamp("2024-01-04", tz="UTC").timestamp() * 1000)
    close_ts = start
    points = parse_nav_eod_df(
        nav,
        symbol_raw="413520",
        venue="akshare",
        market_type="etf",
        start_ms=start,
        end_ms=end,
        close_by_ts={close_ts: 1.10},
    )
    assert len(points) == 1
    assert points[0].source == SOURCE_NAV_EOD
    assert points[0].iopv == 1.05
    assert points[0].price == 1.10
    assert points[0].premium == pytest.approx((1.10 - 1.05) / 1.05)


def test_parse_spot_iopv_filters_and_gates() -> None:
    df = pd.DataFrame(
        [
            {"代码": "513300", "最新价": 1.2, "IOPV实时估值": 1.1},
            {"代码": "413520", "最新价": 1.0, "IOPV实时估值": 0.0},  # 丢弃
            {"代码": "510300", "最新价": 4.0, "IOPV实时估值": 4.0},  # 不在 filter
        ]
    )
    points = parse_spot_iopv_df(
        df,
        symbols=["513300", "413520"],
        venue="akshare",
        market_type="etf",
        ts_event_ms=1_700_000_000_000,
    )
    assert len(points) == 1
    assert points[0].symbol_raw == "513300"
    assert points[0].source == SOURCE_IOPV_REALTIME
    assert points[0].premium == pytest.approx((1.2 - 1.1) / 1.1)


def test_etf_iopv_dedupe_and_query(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path)
    row = EtfIopvPoint(
        venue="akshare",
        market_type="etf",
        instrument_id="akshare:etf:513300",
        symbol_raw="513300",
        ts_event_ms=1_700_000_000_000,
        ts_ingest_ms=1_700_000_000_100,
        iopv=1.0,
        price=1.1,
        premium=0.1,
        source=SOURCE_IOPV_REALTIME,
    )
    store.write_etf_iopv([row])
    store.write_etf_iopv([row.model_copy(update={"price": 1.2, "premium": 0.2, "ts_ingest_ms": 2})])
    rows = load_etf_iopv(tmp_path, venue="akshare", symbol_raw="513300")
    assert len(rows) == 1
    assert rows[0]["price"] == 1.2
    assert rows[0]["source"] == SOURCE_IOPV_REALTIME


@pytest.mark.asyncio
async def test_provider_uses_injected_ak_module() -> None:
    hist = pd.DataFrame(
        [
            {
                "日期": date(2024, 6, 3),
                "开盘": 1.0,
                "收盘": 1.01,
                "最高": 1.02,
                "最低": 0.99,
                "成交量": 10,
                "成交额": 10.1,
            }
        ]
    )
    nav = pd.DataFrame([{"净值日期": date(2024, 6, 3), "单位净值": 1.0}])
    spot = pd.DataFrame(
        [{"代码": "513300", "最新价": 1.05, "IOPV实时估值": 1.0}]
    )

    class FakeAk:
        def fund_etf_hist_em(self, **kwargs):  # noqa: ANN003
            _ = kwargs
            return hist

        def fund_etf_fund_info_em(self, **kwargs):  # noqa: ANN003
            _ = kwargs
            return nav

        def fund_etf_spot_em(self):
            return spot

    provider = AkshareEtfProvider(
        symbols=["513300"],
        request_interval_sec=0,
        ak_module=FakeAk(),
        sleep_fn=lambda _: None,
    )
    start = int(pd.Timestamp("2024-06-01", tz="UTC").timestamp() * 1000)
    end = int(pd.Timestamp("2024-06-10", tz="UTC").timestamp() * 1000)
    bars = await provider.fetch_ohlcv("513300", "1d", start, end)
    assert len(bars) == 1
    nav_pts = await provider.fetch_nav_eod("513300", start, end)
    assert len(nav_pts) == 1
    assert nav_pts[0].price == 1.01
    rt = await provider.fetch_iopv_realtime()
    assert len(rt) == 1
    assert rt[0].premium == pytest.approx(0.05)
    inst = await provider.list_instruments()
    assert inst[0].symbol_raw == "513300"
