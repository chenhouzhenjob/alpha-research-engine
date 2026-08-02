"""Feature and bar-backtest contracts."""

import pandas as pd
import pytest

from alpha.backtest import BacktestConfig, run_backtest
from alpha.cli import main
from alpha.collection import CanonicalStore
from alpha.features import FeatureSet, SmaFeature, compute_features, load_features, write_features
from alpha.schema import OhlcvBar
from alpha.strategies import MovingAverageCrossStrategy


def _bars() -> pd.DataFrame:
    return pd.DataFrame({
        "ts_event_ms": [1, 2, 3, 4], "instrument_id": ["x"] * 4,
        "open": [10.0, 11.0, 12.0, 13.0], "close": [10.0, 11.0, 12.0, 13.0],
    })


def test_feature_store_and_strategy(tmp_path) -> None:
    features = compute_features({"ohlcv": _bars()}, FeatureSet("demo", (SmaFeature(2), SmaFeature(3))))
    assert features.loc[2, "sma_2"] == 11.5
    write_features(tmp_path, "demo", features)
    assert len(load_features(tmp_path, "demo")) == 4
    weights = MovingAverageCrossStrategy("sma_2", "sma_3").target_weights(_bars(), features)
    assert weights.loc[3, "target_weight"] == 1.0


def test_backtest_fills_next_bar_and_writes_order_audit(tmp_path) -> None:
    weights = pd.DataFrame({"ts_event_ms": [1], "instrument_id": ["x"], "target_weight": [1.0]})
    result = run_backtest(_bars(), weights, BacktestConfig(initial_cash=100.0, fee_bps=10))
    assert len(result.orders) == 1
    order = result.orders.iloc[0]
    assert order["ts_fill_ms"] == 2
    assert order["status"] == "filled"
    root = result.write(tmp_path, "audit")
    assert (root / "orders.parquet").exists()
    assert result.summary["total_fees"] > 0


def test_feature_validates_required_datasets() -> None:
    class MacroDependentFeature:
        name = "macro_dependent"
        required_datasets = frozenset({"ohlcv", "macro"})

        def compute(self, inputs):
            return inputs["ohlcv"]["close"]

    with pytest.raises(ValueError, match="macro"):
        compute_features({"ohlcv": _bars()}, FeatureSet("multi-input", (MacroDependentFeature(),)))


def test_backtest_cli_runs_research_yaml(tmp_path) -> None:
    store = CanonicalStore(tmp_path)
    bars = [
        OhlcvBar(
            venue="binance", market_type="perp", instrument_id="binance:perp:BTCUSDT",
            symbol_raw="BTCUSDT", ts_event_ms=idx, ts_ingest_ms=idx, tf="1h",
            open=float(10 + idx), high=float(11 + idx), low=float(9 + idx),
            close=float(10 + idx), volume=1.0,
        )
        for idx in range(1, 25)
    ]
    store.write_ohlcv(bars)
    with pytest.raises(SystemExit, match="0"):
        main([
            "--data-dir", str(tmp_path), "backtest", "run",
            "--research-config", "configs/research/moving_average_cross.yaml",
        ])
    assert (tmp_path / "backtests" / "moving-average-cross" / "orders.parquet").exists()
