from alpha_core.metrics import MetricValue
from alpha_metrics.features.nominal_apr import nominal_apr


def test_sums_fee_and_cake_when_both_available():
    result = nominal_apr(MetricValue.available(0.1), MetricValue.available(0.05))
    assert result.is_available
    assert result.value == 0.15000000000000002 or abs(result.value - 0.15) < 1e-9


def test_treats_no_incentive_cake_as_zero():
    result = nominal_apr(MetricValue.available(0.1), MetricValue.no_incentive())
    assert result.is_available
    assert result.value == 0.1


def test_unavailable_when_fee_apr_unavailable():
    result = nominal_apr(MetricValue.unavailable("no data"), MetricValue.available(0.05))
    assert not result.is_available


def test_unavailable_when_cake_apr_unavailable():
    result = nominal_apr(MetricValue.available(0.1), MetricValue.unavailable("cake price missing"))
    assert not result.is_available
