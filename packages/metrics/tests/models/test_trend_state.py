from alpha_core.metrics import MetricValue
from alpha_metrics.models.trend_state import (
    classify_trend,
    classify_volatility,
    select_maintenance_profile,
)


def test_classify_trend_boundaries():
    assert classify_trend(25.1) == "strong"
    assert classify_trend(25.0) == "transitional"  # 恰好 25：不算 strong（严格大于才算）
    assert classify_trend(20.0) == "transitional"  # 恰好 20：不算 weak（严格小于才算）
    assert classify_trend(19.9) == "weak"
    assert classify_trend(22.5) == "transitional"


def test_classify_volatility_relative_to_own_history():
    history = [1.0, 2.0, 3.0, 4.0, 5.0]  # 中位数 3.0
    assert classify_volatility(3.1, history) == "high"
    assert classify_volatility(3.0, history) == "low"  # 恰好等于中位数算 low（严格大于才算 high）
    assert classify_volatility(1.0, history) == "low"


def test_classify_volatility_empty_history_defaults_low():
    assert classify_volatility(999.0, []) == "low"


def test_select_maintenance_profile_all_six_cells():
    history = [1.0] * 30  # 中位数 1.0

    def _adx(v: float) -> MetricValue[float]:
        return MetricValue.available(v)

    def _atr_pct(v: float, hist: list[float] = history) -> tuple[MetricValue[float], list[float]]:
        return MetricValue.available(v), hist

    cases = [
        (30.0, 2.0, "被动型"),  # strong + high
        (30.0, 0.5, "平衡型"),  # strong + low
        (22.0, 2.0, "平衡型"),  # transitional + high
        (22.0, 0.5, "平衡型"),  # transitional + low
        (10.0, 2.0, "平衡型"),  # weak + high
        (10.0, 0.5, "主动型"),  # weak + low
    ]
    for adx_value, atr_pct_value, expected_profile in cases:
        atr_pct_mv, hist = _atr_pct(atr_pct_value)
        result = select_maintenance_profile(_adx(adx_value), atr_pct_mv, hist)
        assert result.is_available
        assert result.value == expected_profile, f"adx={adx_value}, atr_pct={atr_pct_value}"


def test_select_maintenance_profile_unavailable_when_adx_unavailable():
    result = select_maintenance_profile(
        MetricValue.unavailable("5分钟K线不足"), MetricValue.available(1.0), [1.0] * 30
    )
    assert not result.is_available
    assert "ADX" in result.reason


def test_select_maintenance_profile_unavailable_when_atr_unavailable():
    result = select_maintenance_profile(
        MetricValue.available(30.0), MetricValue.unavailable("5分钟K线不足"), []
    )
    assert not result.is_available
    assert "ATR" in result.reason
