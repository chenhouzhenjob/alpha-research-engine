"""Finnhub 基本面解析单测（无外网）。"""

import json
from pathlib import Path

import pytest

from alpha.integrations.providers.finnhub import (
    parse_earnings_payload,
    parse_financials_payload,
    parse_metric_payload,
    resolve_api_key,
)

FIX = Path(__file__).parent / "fixtures"


def test_parse_financials() -> None:
    data = json.loads((FIX / "finnhub_financials.json").read_text(encoding="utf-8"))
    rows = parse_financials_payload(
        data, symbol_raw="AAPL", statement="ic", frequency="annual"
    )
    assert rows
    assert all(r.venue == "finnhub" for r in rows)
    assert all(r.instrument_id == "finnhub:stock:AAPL" for r in rows)
    assert all(r.statement == "ic" for r in rows)
    # 2023-09-30 UTC
    rev = [r for r in rows if r.metric == "revenue" and r.ts_event_ms == 1696032000000]
    assert len(rev) == 1
    assert rev[0].value == 383285000000


def test_parse_earnings() -> None:
    data = json.loads((FIX / "finnhub_earnings.json").read_text(encoding="utf-8"))
    rows = parse_earnings_payload(data, symbol_raw="AAPL")
    metrics = {r.metric: r.value for r in rows}
    assert metrics["eps_actual"] == 1.52
    assert metrics["eps_estimate"] == 1.43
    assert all(r.statement == "earnings" for r in rows)
    # 2024-03-31
    assert rows[0].ts_event_ms == 1711843200000


def test_parse_metric() -> None:
    data = json.loads((FIX / "finnhub_metric.json").read_text(encoding="utf-8"))
    as_of = 1704067200000  # 2024-01-01
    rows = parse_metric_payload(data, symbol_raw="AAPL", as_of_ms=as_of)
    by_m = {r.metric: r for r in rows}
    assert by_m["pe_ttm"].value == 28.5
    assert by_m["pb"].value == 45.2
    assert by_m["eps_ttm"].value == 6.42
    assert all(r.statement == "metric" for r in rows)
    assert all(r.ts_event_ms == as_of for r in rows)


def test_resolve_api_key(monkeypatch) -> None:
    monkeypatch.setenv("FINNHUB_API_KEY", "fh-key")
    assert resolve_api_key() == "fh-key"
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        resolve_api_key()
