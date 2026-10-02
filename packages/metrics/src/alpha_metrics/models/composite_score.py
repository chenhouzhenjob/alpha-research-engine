"""复合评分（pool-discovery-metrics-v1.md 2. 指标组合）。

    CompositeScore = normalize(NominalAPR)×0.30 + normalize(VTRatio)×0.25
                    − normalize(|ExpectedIL_ref|)×0.25 − normalize(CapitalVolatility)×0.10
                    − AgePenalty×0.10

**符号澄清（原文档没有明说，这里是必要的解读，1e 校准报告里会再提一次）**：ExpectedIL_ref 本身
恒为负数（IL 都是损失）。如果直接对这个带符号的负数做 min-max 归一化，"损失最大"的池子会被
归一化到 0、"损失最小（最接近 0）"的池子被归一化到 1，再乘负号扣分——结果是风险最低的池子被扣得
最多，和公式本意（风险越大扣分越多）正好相反。这里改成对 `|ExpectedIL_ref|`（损失的绝对值/幅度）
做归一化，让"风险越大 → 归一化值越大 → 扣分越多"，是唯一说得通的读法，函数名里用 `il_risk`
指代这个"归一化后的 IL 风险幅度"分项。

**AgePenalty 不参与归一化**：这是原公式里唯一一个不做 min-max、直接用原始值（0 或 0.05）
乘权重扣分的分项，和其余四项性质不同（其余四项都先归一化到 [0,1] 再乘权重），
所以 2.3 节"数据缺失按比例分摊权重"的重新归一逻辑只发生在那四项之间，AgePenalty 单独处理，
它不可得时（池子创建时间未知）就不扣这一项，不参与其余四项的权重重分配。
"""

from __future__ import annotations

from dataclasses import dataclass

# 四个参与归一化 + 加权求和的分项：(权重, 符号)。符号 +1 = 加分项，-1 = 扣分项。
NORMALIZED_COMPONENT_WEIGHTS: dict[str, tuple[float, int]] = {
    "nominal_apr": (0.30, 1),
    "vt_ratio": (0.25, 1),
    "il_risk": (0.25, -1),  # normalize(|ExpectedIL_ref|)
    "capital_volatility": (0.10, -1),
}
AGE_PENALTY_WEIGHT = 0.10  # 单独固定扣分，不参与上面四项的归一化重新分摊

# S/A/B/C 分档阈值，取自 pool-discovery-metrics-v1.md 2.4 节原文——注意这是原文档给的绝对值，
# 不是按本实现"实际可达分数范围"反推校准过的，1e 报告会明确指出两者可能对不上。
TIER_THRESHOLDS: tuple[tuple[str, float], ...] = (("S", 0.75), ("A", 0.55), ("B", 0.35))


def score_tier(score: float) -> str:
    for tier, threshold in TIER_THRESHOLDS:
        if score >= threshold:
            return tier
    return "C"


@dataclass(frozen=True)
class CompositeScoreResult:
    score: float
    confidence: float  # 参与打分的分项数 / 总分项数（5 项：4 个归一化项 + AgePenalty），见 2.3 节
    tier: str


def compute_composite_score(
    normalized_components: dict[str, float | None],
    age_penalty_value: float | None,
) -> CompositeScoreResult:
    """@param normalized_components 键必须是 `NORMALIZED_COMPONENT_WEIGHTS` 的键；
    某个分项该池子不可得时传 None，权重会按比例分摊给其余可用分项（2.3 节）。
    @param age_penalty_value 原始值（0 或 0.05），不是归一化后的值；不可得传 None（不扣这一项）。
    """
    available = {name: value for name, value in normalized_components.items() if value is not None}
    total_weight_available = sum(NORMALIZED_COMPONENT_WEIGHTS[name][0] for name in available)

    score = 0.0
    if total_weight_available > 0:
        for name, value in available.items():
            weight, sign = NORMALIZED_COMPONENT_WEIGHTS[name]
            score += sign * value * (weight / total_weight_available)

    if age_penalty_value is not None:
        score -= age_penalty_value * AGE_PENALTY_WEIGHT

    available_count = len(available) + (1 if age_penalty_value is not None else 0)
    confidence = available_count / (len(NORMALIZED_COMPONENT_WEIGHTS) + 1)

    return CompositeScoreResult(score=score, confidence=confidence, tier=score_tier(score))
