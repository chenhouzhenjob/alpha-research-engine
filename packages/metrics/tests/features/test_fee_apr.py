import math

from alpha_metrics.features.fee_apr import fee_apr


def test_unavailable_when_inputs_missing():
    assert not fee_apr(None, 1_000_000, 2500, 0, 0).is_available
    assert not fee_apr(1_000_000, None, 2500, 0, 0).is_available


def test_unavailable_when_tvl_non_positive():
    assert not fee_apr(1_000_000, 0, 2500, 0, 0).is_available


def test_matches_manual_formula():
    result = fee_apr(
        volume_24h_usd=1_000_000, tvl_usd=1_000_000, fee_pips=2500, fee_protocol0=3300, fee_protocol1=3300
    )
    assert result.is_available
    lp_net_share = 1 - (3300 + 3300) / 2 / 10_000
    expected = (1_000_000 * 2500 / 1_000_000 * lp_net_share) / 1_000_000 * 365
    assert math.isclose(result.value, expected, rel_tol=1e-9)


def test_zero_protocol_fee_gives_full_share():
    result = fee_apr(volume_24h_usd=1_000_000, tvl_usd=1_000_000, fee_pips=2500, fee_protocol0=0, fee_protocol1=0)
    expected = (1_000_000 * 2500 / 1_000_000) / 1_000_000 * 365
    assert math.isclose(result.value, expected, rel_tol=1e-9)
