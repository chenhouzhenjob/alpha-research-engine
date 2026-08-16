from lp_backtest.features.capital_volatility import MIN_TVL_OBSERVATIONS, capital_volatility


def test_unavailable_when_window_too_short():
    result = capital_volatility([100.0] * (MIN_TVL_OBSERVATIONS - 1))
    assert not result.is_available


def test_zero_for_flat_tvl():
    result = capital_volatility([100.0] * MIN_TVL_OBSERVATIONS)
    assert result.is_available
    assert result.value == 0.0


def test_positive_for_fluctuating_tvl():
    series = [100.0, 110.0] * (MIN_TVL_OBSERVATIONS // 2)
    result = capital_volatility(series)
    assert result.is_available
    assert result.value > 0
