"""Hyperliquid 解析单测（无外网）。"""

import json
from pathlib import Path

from alpha.integrations.venues.hyperliquid.market_data import (
    parse_candle,
    parse_funding_hist,
    parse_trade,
)

FIX = Path(__file__).parent / "fixtures"


def test_parse_candle() -> None:
    rows = json.loads((FIX / "hyperliquid_candle.json").read_text(encoding="utf-8"))
    bar = parse_candle(rows[0], market_type="perp")
    assert bar.venue == "hyperliquid"
    assert bar.symbol_raw == "BTC"
    assert bar.ts_event_ms == 1700000000000
    assert bar.tf == "1h"
    assert bar.volume == 123.45


def test_parse_funding_hist() -> None:
    rows = json.loads((FIX / "hyperliquid_funding.json").read_text(encoding="utf-8"))
    fr = parse_funding_hist(rows[0], symbol_raw="BTC", market_type="perp")
    assert fr.funding_rate == 0.0000125
    assert fr.ts_event_ms == 1700006400000


def test_parse_trade_side_b() -> None:
    msg = json.loads((FIX / "hyperliquid_trade_ws.json").read_text(encoding="utf-8"))
    ticks = parse_trade(msg, symbol_raw="BTC", market_type="perp")
    assert len(ticks) == 1
    assert ticks[0].side == "buy"
    assert ticks[0].trade_id == "42"
