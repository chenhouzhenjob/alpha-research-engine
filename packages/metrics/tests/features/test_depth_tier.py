from alpha_core.types import DepthTier
from alpha_metrics.features.depth_tier import depth_tier


def test_unavailable_when_tvl_unknown():
    assert not depth_tier(None).is_available


def test_high_risk_below_100k():
    result = depth_tier(50_000)
    assert result.value == DepthTier.HIGH_RISK


def test_medium_between_100k_and_1m():
    result = depth_tier(500_000)
    assert result.value == DepthTier.MEDIUM


def test_low_risk_above_1m():
    result = depth_tier(5_000_000)
    assert result.value == DepthTier.LOW_RISK
