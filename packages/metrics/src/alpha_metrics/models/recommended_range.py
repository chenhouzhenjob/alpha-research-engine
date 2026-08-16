"""推荐流动性区间宽度（pool-discovery-metrics-v1.md 1.4 节）。

    w_recommended(T_target) = σ_price × √(T_target/365)
    P_upper = P_current × exp(+w_recommended)
    P_lower = P_current × exp(−w_recommended)

三档参考周期，供 1d 回测使用：
- 主动型 T_target=3 天：愿意频繁盯盘换更高手续费捕获效率
- 平衡型 T_target=7 天：默认推荐，和 IL 打分口径（`DEFAULT_T_REF_DAYS`）一致
- 被动型 T_target=30 天：不想频繁操作，接受更低捕获效率换稳定
"""

from __future__ import annotations

import math

# 维护画像 -> 目标跳仓间隔（天），键名用于报告展示。
MAINTENANCE_PROFILES: dict[str, int] = {"主动型": 3, "平衡型": 7, "被动型": 30}


def recommended_width(sigma: float, t_target_days: int) -> float:
    """@returns 对称区间半宽（log 价格），即 `P_upper/P_current` 的自然对数。"""
    return sigma * math.sqrt(t_target_days / 365)


def capital_efficiency(width: float) -> float:
    """集中流动性相对"全范围做市"的资金效率倍数，价格位于区间几何中心时的近似值。

    公式来自 Uniswap V3 白皮书的集中流动性推导：区间 [P₀e⁻ʷ, P₀eʷ] 相对全范围（0,∞）的
    虚拟流动性放大倍数 ≈ `1 / (1 - e⁻ʷ)`。仅在价格位于区间中心时精确，价格偏向区间一侧时
    实际效率会偏离这个值——这里只作为量级参照，不是精确值。

    `width` 趋近于 0（σ_price 极低甚至为 0，如高度锚定的稳定币对）时，理想化模型里区间可以
    做到无限窄而不出界，效率趋于无穷大——这不是可以真实实现的数字，只是极限退化情形，
    这里截断到一个很大但有限的值，避免除零，调用方展示时应当把这类极端值理解为"资金效率极高"，
    不是精确倍数。
    """
    if width <= 1e-9:
        return 1e6
    return 1 / (1 - math.exp(-width))
