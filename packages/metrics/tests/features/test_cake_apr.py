import math

from alpha_core.types import MetricAvailability
from alpha_metrics.features.cake_apr import cake_apr


def test_no_incentive_when_emission_is_none():
    result = cake_apr(None, cake_usd_price=2.0, tvl_usd=1_000_000)
    assert result.availability == MetricAvailability.NO_INCENTIVE
    assert result.value is None


def test_unavailable_when_price_missing():
    result = cake_apr((0.01, 0.5), cake_usd_price=None, tvl_usd=1_000_000)
    assert not result.is_available


def test_unavailable_when_tvl_missing_or_non_positive():
    assert not cake_apr((0.01, 0.5), cake_usd_price=2.0, tvl_usd=None).is_available
    assert not cake_apr((0.01, 0.5), cake_usd_price=2.0, tvl_usd=0).is_available


def test_matches_manual_formula():
    result = cake_apr((0.01, 0.5), cake_usd_price=2.0, tvl_usd=1_000_000)
    assert result.is_available
    expected = (0.01 * 86_400 * 0.5 * 2.0) / 1_000_000 * 365
    assert math.isclose(result.value, expected, rel_tol=1e-9)
