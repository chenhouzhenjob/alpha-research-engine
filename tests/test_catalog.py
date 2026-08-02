"""Catalog watermark 单测。"""

import pytest

from alpha.collection import Catalog
from alpha.schema import Instrument


@pytest.mark.asyncio
async def test_upsert_and_watermark(tmp_path) -> None:
    cat = Catalog(str(tmp_path / "catalog.sqlite"))
    await cat.open()
    try:
        n = await cat.upsert_instruments(
            [
                Instrument(
                    instrument_id="binance:perp:BTCUSDT",
                    venue="binance",
                    market_type="perp",
                    base="BTC",
                    quote="USDT",
                    symbol_raw="BTCUSDT",
                )
            ]
        )
        assert n == 1
        items = await cat.list_instruments("binance")
        assert len(items) == 1
        await cat.set_watermark("binance", "ohlcv", "binance:perp:BTCUSDT", 100, "1h")
        await cat.set_watermark("binance", "ohlcv", "binance:perp:BTCUSDT", 50, "1h")
        assert await cat.get_watermark("binance", "ohlcv", "binance:perp:BTCUSDT", "1h") == 100
    finally:
        await cat.close()


@pytest.mark.asyncio
async def test_legacy_binance_source_watermarks_migrate_to_canonical_venue(tmp_path) -> None:
    path = str(tmp_path / "catalog.sqlite")
    cat = Catalog(path)
    await cat.open()
    await cat.set_watermark("binance", "ohlcv", "binance:perp:BTCUSDT", 50, "1h")
    await cat.set_watermark("binance_perp", "ohlcv", "binance:perp:BTCUSDT", 100, "1h")
    await cat.set_watermark("binance_spot", "ohlcv", "binance:spot:BTCUSDT", 200, "1h")
    await cat.close()

    migrated = Catalog(path)
    await migrated.open()
    try:
        assert await migrated.get_watermark("binance", "ohlcv", "binance:perp:BTCUSDT", "1h") == 100
        assert await migrated.get_watermark("binance", "ohlcv", "binance:spot:BTCUSDT", "1h") == 200
        assert await migrated.get_watermark("binance_perp", "ohlcv", "binance:perp:BTCUSDT", "1h") is None
    finally:
        await migrated.close()
