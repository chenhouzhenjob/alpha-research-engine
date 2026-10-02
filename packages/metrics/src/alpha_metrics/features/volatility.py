"""价格波动率 σ_price（pool-discovery-metrics-v1.md 1.3 节核心风险因子）。

    σ_price = stdev(ln(closeₜ / closeₜ₋₁)) × √365

是 IL 风险、跳仓频率、推荐区间三个指标共用的唯一输入，三者用同一套 GBM 近似模型，保证口径一致。
"""

from __future__ import annotations

import math
import statistics

from alpha_core.metrics import MetricValue

# 原文档"回看窗口建议 30 天"：取 30 个收盘价数据点（对应 29 个日收益率观测），
# 不足则标记 unavailable——不能拿不足样本的方差冒充结果。
MIN_CLOSE_OBSERVATIONS = 30

_TRADING_DAYS_PER_YEAR = 365


def sigma_price(closes: list[float]) -> MetricValue[float]:
    """按时间升序传入的每日收盘价序列，计算"最近 `MIN_CLOSE_OBSERVATIONS` 天"的年化价格波动率。

    传入的序列可以比回看窗口长（比如整段历史），函数会自动只取尾部窗口，
    不会把窗口之外更早的历史也算进方差——语义是"回看窗口"，不是"全部历史"。

    @param closes 按日期升序排列的收盘价（同一池子），要求全部为正数
    @returns 样本不足 `MIN_CLOSE_OBSERVATIONS` 个时标记 unavailable
    """
    if len(closes) < MIN_CLOSE_OBSERVATIONS:
        return MetricValue.unavailable(
            f"回看窗口不足 {MIN_CLOSE_OBSERVATIONS} 天（现有 {len(closes)} 个收盘价）"
        )
    recent = closes[-MIN_CLOSE_OBSERVATIONS:]
    if any(c <= 0 for c in recent):
        return MetricValue.unavailable("收盘价序列存在非正数，无法取对数收益率")

    log_returns = [math.log(recent[i] / recent[i - 1]) for i in range(1, len(recent))]
    daily_stdev = statistics.stdev(log_returns)
    return MetricValue.available(daily_stdev * math.sqrt(_TRADING_DAYS_PER_YEAR))
