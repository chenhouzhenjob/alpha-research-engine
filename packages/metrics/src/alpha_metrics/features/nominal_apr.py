"""名义综合 APR（pool-discovery-metrics-v1.md 1.1 节）。

    NominalAPR = FeeAPR + CakeAPR

这是最直观但最容易误导的数字——不调整风险，纯粹是"如果什么都不变，理论年化是多少"。
不能单独作为排序依据，只作为打分公式（1e 阶段）的一个输入项。

三态合并规则：`CakeAPR` 是 `NO_INCENTIVE`（确认当前没有激励）时按 0 处理，正常求和；
`FeeAPR` 或 `CakeAPR` 是 `UNAVAILABLE`（数据取不到，不是"确认为 0"）时，`NominalAPR` 整体标记 unavailable，
不能悄悄当 0 加总，否则会把"数据缺失"伪装成"确实没有这块收益"。
"""

from __future__ import annotations

from alpha_core.metrics import MetricValue
from alpha_core.types import MetricAvailability


def nominal_apr(fee_apr_value: MetricValue[float], cake_apr_value: MetricValue[float]) -> MetricValue[float]:
    if fee_apr_value.availability == MetricAvailability.UNAVAILABLE:
        return MetricValue.unavailable(f"FeeAPR 不可得: {fee_apr_value.reason}")
    if cake_apr_value.availability == MetricAvailability.UNAVAILABLE:
        return MetricValue.unavailable(f"CakeAPR 不可得: {cake_apr_value.reason}")

    fee = fee_apr_value.value or 0.0
    cake = 0.0 if cake_apr_value.availability == MetricAvailability.NO_INCENTIVE else (cake_apr_value.value or 0.0)
    return MetricValue.available(fee + cake)
