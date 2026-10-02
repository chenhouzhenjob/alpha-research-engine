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


def vt_ratio_ma(daily_ratios: list[float], *, window: int = 7) -> MetricValue[float]:
    """VTRatio 的 N 日移动平均（阶段 1 新增，`live-signal-system-设计方案.md` 第 6 章退出信号 3
    "交易量枯竭"用），默认 7 日——用均值而不是单日值，避免被短期噪音（如周末交易量正常回落）
    误触发（设计文档原话）。

    @param daily_ratios 每日 VTRatio 原始值，按日期升序（调用方从 `PoolMetricsHistoryRow` 历史
    里逐日算 `vt_ratio(volume_24h_usd, tvl_usd)`，只取两者都可得的日子，不足 window 天视为不可用）
    """
    if len(daily_ratios) < window:
        return MetricValue.unavailable(f"VTRatio 历史不足 {window} 天（现有 {len(daily_ratios)} 天）")
    return MetricValue.available(sum(daily_ratios[-window:]) / window)
