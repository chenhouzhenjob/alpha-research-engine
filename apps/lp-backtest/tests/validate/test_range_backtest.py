from datetime import date, timedelta

from alpha_metrics.features.volatility import MIN_CLOSE_OBSERVATIONS
from lp_backtest.validate.range_backtest import _first_breach_offset, collect_range_samples


def _dated_series(closes: list[float]) -> list[tuple[date, float]]:
    start = date(2026, 1, 1)
    return [(start + timedelta(days=i), c) for i, c in enumerate(closes)]


def test_first_breach_offset_none_when_flat():
    closes = [1.0] * 10
    assert _first_breach_offset(closes, 0, width=0.1, max_offset=9) is None


def test_first_breach_offset_detects_upward_break():
    closes = [1.0, 1.0, 1.0, 1.5, 1.0]  # 第 3 天涨 50%，远超一个很窄的区间
    assert _first_breach_offset(closes, 0, width=0.05, max_offset=4) == 3


def test_first_breach_offset_none_when_data_runs_out():
    closes = [1.0, 1.0, 1.0]
    assert _first_breach_offset(closes, 0, width=0.01, max_offset=10) is None


def test_collect_range_samples_empty_when_history_too_short():
    series = _dated_series([1.0] * (MIN_CLOSE_OBSERVATIONS - 1))
    assert collect_range_samples("0xpool", series) == []


def test_collect_range_samples_produces_three_profiles_per_anchor():
    total_days = MIN_CLOSE_OBSERVATIONS + 40
    series = _dated_series([1.0] * total_days)
    samples = collect_range_samples("0xpool", series, stride_days=1000)  # 只取一个锚点
    assert len(samples) == 3  # 主动/平衡/被动三档
    profiles = {s.profile for s in samples}
    assert profiles == {"主动型", "平衡型", "被动型"}
    for s in samples:
        assert s.sigma_price == 0.0
        assert s.utilization_ratio == 1.0  # 价格是平的，全程没出界
        assert not s.breached_within_target
