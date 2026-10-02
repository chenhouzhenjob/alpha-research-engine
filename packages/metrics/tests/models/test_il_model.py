import math

from alpha_metrics.models.il_model import expected_il_ref, realized_il_from_price_ratio


def test_realized_il_zero_when_price_unchanged():
    assert realized_il_from_price_ratio(1.0) == 0.0


def test_realized_il_symmetric_under_inverse_ratio():
    forward = realized_il_from_price_ratio(1.5)
    backward = realized_il_from_price_ratio(1 / 1.5)
    assert math.isclose(forward, backward, rel_tol=1e-9)
    assert forward < 0  # IL 总是非正


def test_expected_il_ref_zero_when_sigma_zero():
    assert expected_il_ref(0.0) == 0.0


def test_expected_il_ref_matches_manual_formula():
    sigma = 0.8
    t_ref_days = 7
    k = math.exp(sigma * math.sqrt(t_ref_days / 365))
    expected = 2 * math.sqrt(k) / (1 + k) - 1
    assert math.isclose(expected_il_ref(sigma, t_ref_days=t_ref_days), expected, rel_tol=1e-9)
