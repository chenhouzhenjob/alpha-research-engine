from lp_backtest.features.volatility import MIN_CLOSE_OBSERVATIONS, sigma_price


def test_unavailable_when_window_too_short():
    result = sigma_price([1.0] * (MIN_CLOSE_OBSERVATIONS - 1))
    assert not result.is_available
    assert "不足" in result.reason


def test_zero_volatility_for_flat_prices():
    result = sigma_price([1.0] * MIN_CLOSE_OBSERVATIONS)
    assert result.is_available
    assert result.value == 0.0


def test_unavailable_on_non_positive_price():
    result = sigma_price([1.0] * (MIN_CLOSE_OBSERVATIONS - 1) + [0.0])
    assert not result.is_available
