from alpha_metrics.features.vt_ratio import vt_ratio, vt_ratio_ma


def test_unavailable_when_inputs_missing():
    assert not vt_ratio(None, 1000.0).is_available
    assert not vt_ratio(100.0, None).is_available


def test_unavailable_when_tvl_non_positive():
    assert not vt_ratio(100.0, 0.0).is_available


def test_computes_ratio():
    result = vt_ratio(100.0, 1000.0)
    assert result.is_available
    assert result.value == 0.1


def test_vt_ratio_ma_unavailable_below_window():
    result = vt_ratio_ma([0.1, 0.2, 0.3], window=7)
    assert not result.is_available
    assert "7" in result.reason


def test_vt_ratio_ma_averages_last_window_days():
    # 前面多出来的 [999.0] 不该被算进去——只取最近 window 天。
    daily_ratios = [999.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
    result = vt_ratio_ma(daily_ratios, window=7)
    assert result.is_available
    assert result.value == sum([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]) / 7
