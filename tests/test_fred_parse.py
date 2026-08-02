"""FRED 宏观解析单测（无外网）。"""

import json
from pathlib import Path

import pytest

from alpha.integrations.providers.fred import parse_observations_payload, resolve_api_key

FIX = Path(__file__).parent / "fixtures"


def test_parse_observations_skips_missing() -> None:
    data = json.loads((FIX / "fred_observations.json").read_text(encoding="utf-8"))
    rows = parse_observations_payload(
        data, series_id="GDP", frequency="quarterly", unit="billions_usd"
    )
    assert len(rows) == 2
    assert rows[0].instrument_id == "fred:macro:GDP"
    assert rows[0].symbol_raw == "GDP"
    assert rows[0].metric == "value"
    assert rows[0].value == 26465.865
    assert rows[0].ts_event_ms == 1672531200000  # 2023-01-01
    assert rows[1].value == 27610.128
    assert all(r.frequency == "quarterly" for r in rows)
    assert all(r.unit == "billions_usd" for r in rows)


def test_resolve_api_key(monkeypatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "fred-key")
    assert resolve_api_key() == "fred-key"
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        resolve_api_key()
