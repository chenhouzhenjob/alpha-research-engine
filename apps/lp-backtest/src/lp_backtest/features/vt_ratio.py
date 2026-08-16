"""Volume/TVL 比值（pool-discovery-metrics-v1.md 1.2 节）。

    VTRatio = 24h volume / TVL

比 APR 更早、更可靠的信号：TVL 低估、APR 虚高的池子会在这里露馅。建议作为候选池初筛的
第一道分数，而不只是复合分里的一项权重（复合分权重校准是 1e 阶段的事，这里只算原始值）。
"""

from __future__ import annotations

from alpha_core.metrics import MetricValue


def vt_ratio(volume_24h_usd: float | None, tvl_usd: float | None) -> MetricValue[float]:
    if volume_24h_usd is None or tvl_usd is None:
        return MetricValue.unavailable("volume_24h_usd 或 tvl_usd 未知")
    if tvl_usd <= 0:
        return MetricValue.unavailable("TVL 非正数，VTRatio 无意义")
    return MetricValue.available(volume_24h_usd / tvl_usd)
