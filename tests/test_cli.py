from alpha.cli import _resolve_sources, build_parser
from alpha.config import load_collect


def test_force_ignores_watermark_for_time_window_commands() -> None:
    args = build_parser().parse_args([
        "collect", "backfill", "ohlcv", "--venue", "binance", "--days", "7", "--force"
    ])
    assert args.force is True


def test_binance_venue_selects_both_market_profiles_by_default() -> None:
    args = build_parser().parse_args(["collect", "backfill", "ohlcv", "--venue", "binance"])
    assert _resolve_sources(load_collect(), args) == ["binance_perp", "binance_spot"]

    spot_args = build_parser().parse_args([
        "collect", "backfill", "ohlcv", "--venue", "binance", "--market-type", "spot"
    ])
    assert _resolve_sources(load_collect(), spot_args) == ["binance_spot"]
