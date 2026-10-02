"""TVL 深度分档（pool-discovery-metrics-v1.md 1.3 节）。

浅池子看起来 APR 高，但进不去多少资金、退出滑点大，单独标注深度分档供用户判断
"这个 APR 我能吃到多少"。不参与复合分公式，是独立展示信息。
"""

from __future__ import annotations

from alpha_core.metrics import MetricValue
from alpha_core.types import DepthTier

_HIGH_RISK_CEILING_USD = 100_000
_MEDIUM_CEILING_USD = 1_000_000


def depth_tier(tvl_usd: float | None) -> MetricValue[DepthTier]:
    if tvl_usd is None:
        return MetricValue.unavailable("TVL 未知")
    if tvl_usd < _HIGH_RISK_CEILING_USD:
        return MetricValue.available(DepthTier.HIGH_RISK)
    if tvl_usd < _MEDIUM_CEILING_USD:
        return MetricValue.available(DepthTier.MEDIUM)
    return MetricValue.available(DepthTier.LOW_RISK)
