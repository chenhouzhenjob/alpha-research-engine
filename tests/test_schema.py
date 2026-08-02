"""schema 与 instrument_id 单测。"""

from alpha.schema import OhlcvBar, make_instrument_id


def test_make_instrument_id() -> None:
    assert make_instrument_id("binance", "perp", "BTCUSDT") == "binance:perp:BTCUSDT"


def test_ohlcv_defaults() -> None:
    bar = OhlcvBar(
        venue="binance",
        market_type="perp",
        instrument_id="binance:perp:BTCUSDT",
        symbol_raw="BTCUSDT",
        ts_event_ms=1,
        ts_ingest_ms=2,
        tf="1h",
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10.0,
    )
    assert bar.dataset == "ohlcv"
    assert bar.schema_version == 1
