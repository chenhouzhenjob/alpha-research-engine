"""币安解析单测（无外网）。"""

import json
from pathlib import Path

from alpha.integrations.venues.binance.market_data import (
    parse_funding,
    parse_kline,
    parse_trade_event,
)

FIX = Path(__file__).parent / "fixtures"


def test_parse_kline() -> None:
    rows = json.loads((FIX / "binance_klines.json").read_text(encoding="utf-8"))
    bar = parse_kline(rows[0], symbol_raw="BTCUSDT", tf="1h", market_type="perp")
    assert bar.venue == "binance"
    assert bar.ts_event_ms == 1700000000000
    assert bar.open == 37000.1
    assert bar.trade_count == 888
    assert bar.instrument_id == "binance:perp:BTCUSDT"


def test_parse_funding() -> None:
    rows = json.loads((FIX / "binance_funding.json").read_text(encoding="utf-8"))
    fr = parse_funding(rows[0], market_type="perp")
    assert fr.funding_rate == 0.0001
    assert fr.mark_price == 37000.0
    assert fr.symbol_raw == "BTCUSDT"


def test_parse_agg_trade_buyer_maker_is_sell() -> None:
    msg = json.loads((FIX / "binance_agg_trade.json").read_text(encoding="utf-8"))
    tick = parse_trade_event(msg, symbol_raw="BTCUSDT", market_type="perp")
    assert tick.side == "sell"
    assert tick.is_buyer_maker is True
    assert tick.trade_id == "123456789"


def test_parse_trade_event_uses_t() -> None:
    msg = json.loads((FIX / "binance_trade.json").read_text(encoding="utf-8"))
    tick = parse_trade_event(msg, symbol_raw="BTCUSDT", market_type="perp")
    assert tick.trade_id == "987654321"
    assert tick.side == "sell"
