"""Parquet 去重与 query 单测。"""

from pathlib import Path

from alpha.collection import CanonicalStore, load_ohlcv
from alpha.schema import OhlcvBar


def test_ohlcv_dedupe_and_query(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path)
    bar = OhlcvBar(
        venue="binance",
        market_type="perp",
        instrument_id="binance:perp:BTCUSDT",
        symbol_raw="BTCUSDT",
        ts_event_ms=1700000000000,
        ts_ingest_ms=1700000001000,
        tf="1h",
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10.0,
    )
    store.write_ohlcv([bar])
    # 再次写入应去重
    bar2 = bar.model_copy(update={"close": 1.6, "ts_ingest_ms": 1700000002000})
    store.write_ohlcv([bar2])

    rows = load_ohlcv(tmp_path, venue="binance", tf="1h")
    assert len(rows) == 1
    assert rows[0]["close"] == 1.6
