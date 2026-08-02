"""Alpaca bars / trades / quotes 解析单测（无外网）。"""

import json
from pathlib import Path

import pytest

from alpha.integrations.providers.alpaca import (
    parse_bar,
    parse_quote,
    parse_trade_hist,
    resolve_credentials,
)

FIX = Path(__file__).parent / "fixtures"


def test_parse_bar() -> None:
    data = json.loads((FIX / "alpaca_bars.json").read_text(encoding="utf-8"))
    bar = parse_bar(data["bars"][0], symbol_raw="AAPL", tf="1d", market_type="stock")
    assert bar.venue == "alpaca"
    assert bar.market_type == "stock"
    assert bar.instrument_id == "alpaca:stock:AAPL"
    assert bar.open == 187.15
    assert bar.close == 185.64
    assert bar.trade_count == 712345
    # 2024-01-02T05:00:00Z
    assert bar.ts_event_ms == 1704171600000


def test_parse_trade_hist() -> None:
    data = json.loads((FIX / "alpaca_trades.json").read_text(encoding="utf-8"))
    tick = parse_trade_hist(
        data["trades"][0], symbol_raw="AAPL", market_type="stock", feed="sip"
    )
    assert tick.venue == "alpaca"
    assert tick.instrument_id == "alpaca:stock:AAPL"
    assert tick.price == 185.5
    assert tick.size == 100
    assert tick.trade_id == "52983525027154"
    assert tick.side == "unknown"
    assert tick.feed == "sip"
    # 2024-01-02T14:30:00.123456Z → ms
    assert tick.ts_event_ms == 1704205800123


def test_parse_quote() -> None:
    data = json.loads((FIX / "alpaca_quotes.json").read_text(encoding="utf-8"))
    q = parse_quote(
        data["quotes"][0], symbol_raw="AAPL", market_type="stock", feed="sip"
    )
    assert q.dataset == "quote"
    assert q.bid_px == 185.5
    assert q.bid_sz == 100
    assert q.ask_px == 185.55
    assert q.ask_sz == 200
    assert q.feed == "sip"
    # 2024-01-02T14:30:00.500000Z
    assert q.ts_event_ms == 1704205800500


def test_resolve_credentials_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "env-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "env-secret")
    key, secret = resolve_credentials()
    assert key == "env-key"
    assert secret == "env-secret"


def test_resolve_credentials_missing(monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(SystemExit):
        resolve_credentials()
