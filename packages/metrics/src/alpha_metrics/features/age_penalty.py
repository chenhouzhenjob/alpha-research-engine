"""池龄惩罚（pool-discovery-metrics-v1.md 1.3 节）。

    AgePenalty = 池龄 < 30天 ? 0.05 : 0

新池子数据不稳定、也是钓鱼盘高发期，用固定惩罚项压低排名而非直接剔除
（准入门槛已经拦掉 < 7 天的池子，这里对 7-30 天的池子做进一步降权）。
"""

from __future__ import annotations

from datetime import datetime

from alpha_core.metrics import MetricValue

AGE_PENALTY_THRESHOLD_DAYS = 30
AGE_PENALTY_VALUE = 0.05


def age_penalty(created_at: datetime | None, as_of: datetime) -> MetricValue[float]:
    """@param created_at 池子创建时间；未知（尚未从链上/数据源回填）时标记 unavailable。"""
    if created_at is None:
        return MetricValue.unavailable("池子创建时间未知")
    age_days = (as_of - created_at).days
    return MetricValue.available(AGE_PENALTY_VALUE if age_days < AGE_PENALTY_THRESHOLD_DAYS else 0.0)
