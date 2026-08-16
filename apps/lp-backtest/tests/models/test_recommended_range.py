import math

from lp_backtest.models.recommended_range import capital_efficiency, recommended_width


def test_recommended_width_scales_with_sqrt_time():
    sigma = 0.5
    w7 = recommended_width(sigma, 7)
    w28 = recommended_width(sigma, 28)
    assert math.isclose(w28, w7 * 2, rel_tol=1e-9)  # 4 倍时间 -> sqrt(4)=2 倍宽度


def test_capital_efficiency_increases_as_width_shrinks():
    assert capital_efficiency(0.01) > capital_efficiency(0.1) > capital_efficiency(1.0)


def test_capital_efficiency_matches_known_uniswap_v3_approximation():
    # 官方常引用的量级参照：约 10% 半宽对应约 10 倍资金效率。
    assert math.isclose(capital_efficiency(0.1), 10.5, abs_tol=0.1)


def test_capital_efficiency_does_not_divide_by_zero_at_zero_width():
    # σ_price=0（完全没有波动，如高度锚定的稳定币对）时 width=0，不能直接除零崩溃。
    assert capital_efficiency(0.0) > 0
    assert math.isfinite(capital_efficiency(0.0))
