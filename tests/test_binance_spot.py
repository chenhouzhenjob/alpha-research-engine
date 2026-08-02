from alpha.config import load_source
from alpha.integrations.registry import MARKET_DATA_ADAPTERS
from alpha.integrations.venues.binance.market_data import parse_kline
from alpha.integrations.venues.binance.spot_market_data import BinanceSpotAdapter


def test_binance_spot_is_registered_and_uses_spot_canonical_identity() -> None:
    assert MARKET_DATA_ADAPTERS["binance_spot"] is BinanceSpotAdapter
    bar = parse_kline(
        [1, "1", "2", "0.5", "1.5", "10", 2, "15", 3],
        symbol_raw="BTCUSDT", tf="1h", market_type="spot",
    )
    assert bar.instrument_id == "binance:spot:BTCUSDT"


def test_binance_market_profiles_share_one_config_file() -> None:
    perp = load_source("binance_perp")
    spot = load_source("binance_spot")
    assert perp["venue"] == spot["venue"] == "binance"
    assert perp["market_type"] == "perp"
    assert spot["market_type"] == "spot"
