"""资金流波动率（TVL/Volume 稳定性，pool-discovery-metrics-v1.md 1.3 节独立信号，权重降低）。

    CapitalVolatility = stdev(近14日 TVL 日环比变化率)

和价格波动率是两回事：这个反映"大户资金频繁进出、池子本身脆不脆弱"，
价格波动率反映"LP 头寸本身会不会亏 IL"。不做年化——原文档没有 ×√365，是短周期稳定性信号，不是风险定价输入。
"""

from __future__ import annotations

import statistics

from alpha_core.metrics import MetricValue

# "近 14 日"取 14 个 TVL 观测点（对应 13 个日环比变化率），依赖 `pool_metrics_history` 时间序列，
# 样本不足标记 unavailable（沿用 1a README 的已知局限：TVL 历史要逐日 ingest 才能积累出来）。
MIN_TVL_OBSERVATIONS = 14


def capital_volatility(tvl_series: list[float]) -> MetricValue[float]:
    """按时间升序传入的每日 TVL 序列，计算近 14 日 TVL 日环比变化率的标准差。

    @param tvl_series 按日期升序排列的 TVL（USD），要求全部为正数
    """
    if len(tvl_series) < MIN_TVL_OBSERVATIONS:
        return MetricValue.unavailable(
            f"TVL 历史样本不足 {MIN_TVL_OBSERVATIONS} 天（现有 {len(tvl_series)} 个观测点）"
        )
    if any(v <= 0 for v in tvl_series):
        return MetricValue.unavailable("TVL 序列存在非正数，无法计算环比变化率")

    recent = tvl_series[-MIN_TVL_OBSERVATIONS:]
    pct_changes = [
        (recent[i] - recent[i - 1]) / recent[i - 1] for i in range(1, len(recent))
    ]
    return MetricValue.available(statistics.stdev(pct_changes))
