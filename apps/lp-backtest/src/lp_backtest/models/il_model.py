"""IL（无常损失）相关公式（pool-discovery-metrics-v1.md 1.3 节）。

`realized_il_from_price_ratio` 是标准 IL 公式本身，`expected_il_ref` 是用它加上
σ_price 驱动的"一个标准差价格波动"代入得到的近似期望值——两者共用同一个核心公式，
1c 阶段验证"预测 IL vs 实际 IL"时，预测值来自 `expected_il_ref`，实际值来自
`realized_il_from_price_ratio`，公式一致，可比。
"""

from __future__ import annotations

import math

from alpha_core.metrics import MetricValue

from ..features.volatility import sigma_price as compute_sigma_price

# 复合分打分口径统一用 7 天参考周期，与用户实际选的区间宽度无关（见 1.3 节）。
DEFAULT_T_REF_DAYS = 7
_DAYS_PER_YEAR = 365


def realized_il_from_price_ratio(price_ratio: float) -> float:
    """标准 IL 公式：`IL% = 2√(priceRatio)/(1+priceRatio) − 1`。

    对称——`price_ratio` 和它的倒数算出同一个结果，调用方不需要关心传的是
    "结束价/起始价"还是反过来。

    @param price_ratio 必须为正数
    @returns 无常损失比例（负数，如 -0.006 表示亏 0.6%）
    """
    if price_ratio <= 0:
        raise ValueError(f"price_ratio 必须为正数，收到 {price_ratio}")
    return 2 * math.sqrt(price_ratio) / (1 + price_ratio) - 1


def expected_il_ref(sigma: float, *, t_ref_days: int = DEFAULT_T_REF_DAYS) -> float:
    """用"1 个标准差价格波动"代入标准 IL 公式做近似期望值，不是对全概率分布积分的严格期望。

    @param sigma 年化价格波动率（`sigma_price` 的计算结果）
    @param t_ref_days 参考周期，默认 7 天，是打分口径，与用户实际区间宽度无关
    """
    k = math.exp(sigma * math.sqrt(t_ref_days / _DAYS_PER_YEAR))
    return realized_il_from_price_ratio(k)


def expected_il_ref_from_closes(
    closes: list[float], *, t_ref_days: int = DEFAULT_T_REF_DAYS
) -> MetricValue[float]:
    """先算 σ_price 再套 `expected_il_ref`，σ_price 不可得时原样透传 unavailable。"""
    sigma = compute_sigma_price(closes)
    if not sigma.is_available:
        return MetricValue.unavailable(sigma.reason or "σ_price 不可得")
    return MetricValue.available(expected_il_ref(sigma.value, t_ref_days=t_ref_days))
