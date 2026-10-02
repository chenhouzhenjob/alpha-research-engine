"""退出信号模型，阶段 1 新增（`live-signal-system-设计方案.md` 第 6 章，对应
`pool-discovery-metrics-v1.md` §3.2 的 5 条信号）。发现框架看的是候选集内的相对排名，
这里换成"相对开仓时/相对近期均值的退化趋势"——同样是"分低"，语义不同（见 §3.1）。

5 条信号里，信号 1（NetEdge）、2（CAKE 撤出）、4（排名坍塌）是纯池子级别信号，不需要仓位
数据；信号 3（VTRatio 枯竭）、5（累计倒亏）需要"开仓那一刻"的基线值，由调用方
（alpha-lp，它自己有 `positions` 表）通过 `/conclusion` 的可选 query 参数传入——不传就是
`unavailable`，不阻塞其余信号。

信号 3/4 只暴露原始值（`vtratio_ma7_drop_pct`/`composite_rank_percentile`），不在这里内置
"是否触发"的布尔字段——阈值本身就是设计文档"如 -50%"/"如后 50%"这种带"如"字的例子，不是
精确要求，`any_signal_fired` 用这里定义的默认阈值判断，调用方如果想用别的阈值可以自己重新
判断这两个原始值，不需要改这个模型的返回结构。
"""

from __future__ import annotations

from dataclasses import dataclass

from alpha_core.metrics import MetricValue
from alpha_core.types import MetricAvailability
from alpha_storage.models import PoolMetricsHistoryRow

from ..compute import PoolDailyMetrics
from ..features.vt_ratio import vt_ratio, vt_ratio_ma
from .il_model import realized_il_from_price_ratio

_DAYS_PER_YEAR = 365
NET_EDGE_T_REF_DAYS = 7  # 跟 il_model.DEFAULT_T_REF_DAYS 打分口径一致，不是另起一个周期

# 两条"只给原始值不给布尔"的信号的默认判定阈值——都是设计文档"如"字后面的例子，不是精确要求。
VTRATIO_MA7_DROP_THRESHOLD = -0.5
COMPOSITE_RANK_PERCENTILE_THRESHOLD = 0.5


@dataclass(frozen=True)
class ExitSignals:
    net_edge_negative: MetricValue[bool]
    net_edge_value: MetricValue[float]
    cake_incentive_withdrawn: MetricValue[bool]
    vtratio_ma7_drop_pct: MetricValue[float]
    composite_rank_percentile: MetricValue[float]
    cumulative_realized_il_exceeds_fees: MetricValue[bool]
    realized_il_usd: MetricValue[float]


def _net_edge(daily_metrics: PoolDailyMetrics) -> tuple[MetricValue[bool], MetricValue[float]]:
    """信号 1：`NetEdge = income_over_Tref + ExpectedIL_ref`（IL 恒为负数）。两项都是每次请求
    现算的（`compute_daily_metrics` 每次都重新算），天然满足"用当前 σ_price 重新计算，而非
    开仓时的旧值"的要求，不需要额外传参。
    """
    if not daily_metrics.nominal_apr.is_available:
        reason = f"NominalAPR 不可得：{daily_metrics.nominal_apr.reason}"
        return MetricValue.unavailable(reason), MetricValue.unavailable(reason)
    if not daily_metrics.expected_il_ref.is_available:
        reason = f"ExpectedIL_ref 不可得：{daily_metrics.expected_il_ref.reason}"
        return MetricValue.unavailable(reason), MetricValue.unavailable(reason)

    income_over_t_ref = daily_metrics.nominal_apr.value * (NET_EDGE_T_REF_DAYS / _DAYS_PER_YEAR)
    net_edge = income_over_t_ref + daily_metrics.expected_il_ref.value
    return MetricValue.available(net_edge < 0), MetricValue.available(net_edge)


def _cake_incentive_withdrawn(daily_metrics: PoolDailyMetrics) -> MetricValue[bool]:
    """信号 2：CakeAPR 从 `available` 变成 `no_incentive`。**已知局限**：没有仓位基线的话，
    分不清"这个池子从来没有 CAKE"和"CAKE 被撤了"——两种情况看起来一样，阶段 1 接受这个局限
    （大概率两种情况都不该继续做市，误判后果不严重）。`cake_apr` 本身读不到（`unavailable`，
    如链上调用失败）时，这条信号也标记不可用——不能在"到底有没有激励"都不确定的情况下断言
    "被撤了"或"没被撤"。
    """
    if daily_metrics.cake_apr.availability == MetricAvailability.UNAVAILABLE:
        return MetricValue.unavailable(f"CakeAPR 不可得：{daily_metrics.cake_apr.reason}")
    return MetricValue.available(daily_metrics.cake_apr.availability == MetricAvailability.NO_INCENTIVE)


def _vtratio_ma7_drop_pct(
    history: list[PoolMetricsHistoryRow], position_open_vtratio_ma7: float | None
) -> MetricValue[float]:
    """信号 3：当前 VTRatio_MA(7日) 相比开仓时的相对变化（负数=下降）。`history` 复用
    `PoolMetricsRepository.get_series_up_to` 已经查出来的同一份列表，不需要新查询。
    """
    if position_open_vtratio_ma7 is None:
        return MetricValue.unavailable("未传 position_open_vtratio_ma7（开仓时基线未知）")
    if position_open_vtratio_ma7 <= 0:
        return MetricValue.unavailable("position_open_vtratio_ma7 非正数，无法算相对变化")

    daily_ratios = [vt_ratio(r.volume_24h_usd, r.tvl_usd) for r in history]
    available_ratios = [r.value for r in daily_ratios if r.is_available]
    current_ma7 = vt_ratio_ma(available_ratios, window=7)
    if not current_ma7.is_available:
        return MetricValue.unavailable(current_ma7.reason or "当前 VTRatio_MA7 不可得")

    drop_pct = (current_ma7.value - position_open_vtratio_ma7) / position_open_vtratio_ma7
    return MetricValue.available(drop_pct)


def _cumulative_realized_il(
    current_price: float | None,
    position_open_price: float | None,
    position_open_value_usd: float | None,
    position_cumulative_fees_usd: float | None,
) -> tuple[MetricValue[bool], MetricValue[float]]:
    """信号 5：累计已实现 IL（美元）是否超过累计手续费+CAKE 收入（美元）——不依赖 GBM 假设或
    波动率模型，只用实际发生的价格变化和实际到手的收入对比，设计文档原文建议在几条信号里
    权重最高。
    """
    if current_price is None:
        reason = "当前价格未知（链上 slot0 读取失败）"
        return MetricValue.unavailable(reason), MetricValue.unavailable(reason)
    if (
        position_open_price is None
        or position_open_value_usd is None
        or position_cumulative_fees_usd is None
    ):
        reason = (
            "未传开仓基线（position_open_price/position_open_value_usd/"
            "position_cumulative_fees_usd 三者缺一不可）"
        )
        return MetricValue.unavailable(reason), MetricValue.unavailable(reason)
    if position_open_price <= 0:
        reason = "position_open_price 非正数"
        return MetricValue.unavailable(reason), MetricValue.unavailable(reason)

    price_ratio = current_price / position_open_price
    realized_il_pct = realized_il_from_price_ratio(price_ratio)
    realized_il_usd = abs(realized_il_pct) * position_open_value_usd
    fired = realized_il_usd > position_cumulative_fees_usd
    return MetricValue.available(fired), MetricValue.available(realized_il_usd)


def compute_exit_signals(
    *,
    daily_metrics: PoolDailyMetrics,
    history: list[PoolMetricsHistoryRow],
    current_price: float | None,
    composite_rank_percentile: MetricValue[float],
    position_open_vtratio_ma7: float | None,
    position_open_price: float | None,
    position_open_value_usd: float | None,
    position_cumulative_fees_usd: float | None,
) -> ExitSignals:
    """5 条信号的编排入口。`composite_rank_percentile`（信号 4）由调用方用
    `scoring.compute_percentile_ranks` 对全量候选池算好传入——这里不自己算，避免 models/
    反过来依赖 scoring.py（scoring 在 models 之上编排，不是反过来）。
    """
    net_edge_negative, net_edge_value = _net_edge(daily_metrics)
    cake_incentive_withdrawn = _cake_incentive_withdrawn(daily_metrics)
    vtratio_ma7_drop_pct = _vtratio_ma7_drop_pct(history, position_open_vtratio_ma7)
    cumulative_realized_il_exceeds_fees, realized_il_usd = _cumulative_realized_il(
        current_price, position_open_price, position_open_value_usd, position_cumulative_fees_usd
    )
    return ExitSignals(
        net_edge_negative=net_edge_negative,
        net_edge_value=net_edge_value,
        cake_incentive_withdrawn=cake_incentive_withdrawn,
        vtratio_ma7_drop_pct=vtratio_ma7_drop_pct,
        composite_rank_percentile=composite_rank_percentile,
        cumulative_realized_il_exceeds_fees=cumulative_realized_il_exceeds_fees,
        realized_il_usd=realized_il_usd,
    )


def any_signal_fired(signals: ExitSignals) -> bool:
    """5 条信号里任意一条"可用且触发" → True，供 `/conclusion` 的 `recommended_action`
    判断用（任一触发 → `exit`，否则退回现有 tier 映射）。
    """
    if signals.net_edge_negative.is_available and signals.net_edge_negative.value:
        return True
    if signals.cake_incentive_withdrawn.is_available and signals.cake_incentive_withdrawn.value:
        return True
    if (
        signals.vtratio_ma7_drop_pct.is_available
        and signals.vtratio_ma7_drop_pct.value <= VTRATIO_MA7_DROP_THRESHOLD
    ):
        return True
    if (
        signals.composite_rank_percentile.is_available
        and signals.composite_rank_percentile.value < COMPOSITE_RANK_PERCENTILE_THRESHOLD
    ):
        return True
    return bool(
        signals.cumulative_realized_il_exceeds_fees.is_available
        and signals.cumulative_realized_il_exceeds_fees.value
    )
