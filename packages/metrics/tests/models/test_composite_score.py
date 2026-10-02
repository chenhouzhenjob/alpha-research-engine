import math

from alpha_metrics.models.composite_score import compute_composite_score, score_tier


def test_all_components_available():
    result = compute_composite_score(
        {"nominal_apr": 1.0, "vt_ratio": 1.0, "il_risk": 0.0, "capital_volatility": 0.0},
        age_penalty_value=0.0,
    )
    # 最佳情形下的理论上限：(0.30+0.25)/(0.30+0.25+0.25+0.10) = 0.55/0.90 ≈ 0.6111
    assert math.isclose(result.score, 0.55 / 0.90, rel_tol=1e-9)
    assert result.confidence == 1.0


def test_worst_case_is_negative():
    result = compute_composite_score(
        {"nominal_apr": 0.0, "vt_ratio": 0.0, "il_risk": 1.0, "capital_volatility": 1.0},
        age_penalty_value=0.05,
    )
    assert result.score < 0
    assert result.tier == "C"


def test_missing_component_redistributes_weight_not_treated_as_zero():
    # 缺一项（capital_volatility）不该按 0 处理，而是把它的权重分摊给其余可用分项。
    with_all = compute_composite_score(
        {"nominal_apr": 0.8, "vt_ratio": 0.6, "il_risk": 0.2, "capital_volatility": 0.0},
        age_penalty_value=0.0,
    )
    missing_one = compute_composite_score(
        {"nominal_apr": 0.8, "vt_ratio": 0.6, "il_risk": 0.2, "capital_volatility": None},
        age_penalty_value=0.0,
    )
    # 分数不应该恰好相等（重新分摊后其余权重变大），也不应该是"当缺失项=0"那种巧合。
    assert with_all.score != missing_one.score
    assert missing_one.confidence == 0.8  # 4/5 个分项可得


def test_confidence_counts_age_penalty_as_one_of_five():
    result = compute_composite_score(
        {"nominal_apr": 0.5, "vt_ratio": None, "il_risk": None, "capital_volatility": None},
        age_penalty_value=None,
    )
    assert result.confidence == 0.2  # 只有 nominal_apr 一项可得，1/5


def test_score_tier_boundaries():
    assert score_tier(0.75) == "S"
    assert score_tier(0.74999) == "A"
    assert score_tier(0.55) == "A"
    assert score_tier(0.35) == "B"
    assert score_tier(0.34999) == "C"
