"""组装层：对任意候选池、任意历史日期，把特征层（`features/`）和模型层（`models/`）的输出
拼成一个完整的信号包。本身既不是特征也不是模型，是给上层（未来 alpha-lp 决策层）的统一入口。

**已知限制**（务必先读）：`fee_protocol`（协议抽成）、`cake_emission`（CAKE 排放）、
`cake_usd_price` 三项永远是"调用时刻的链上/行情当前值"，不是 `as_of` 那一天的历史值——
链上没有把这三项的历史变化记录下来（协议抽成极少变、CAKE 排放没有历史事件可查，见
apps/lp-backtest/README.md 的调研结论），所以对历史日期算出来的 FeeAPR/CakeAPR
本质上是"用今天的费率/排放/价格，套那一天的 volume/TVL"，不是严格意义上的历史重现。
其余指标（VTRatio/σ_price/ExpectedIL_ref/CapitalVolatility/AgePenalty/DepthTier）
用的都是 `pool_metrics_history` 里那一天的真实历史数据，没有这个限制。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from alpha_core.metrics import MetricValue
from alpha_core.types import Chain, DepthTier
from alpha_storage.models import PoolMetricsHistoryRow

from .features.age_penalty import age_penalty
from .features.cake_apr import cake_apr
from .features.capital_volatility import capital_volatility
from .features.depth_tier import depth_tier
from .features.fee_apr import fee_apr
from .features.nominal_apr import nominal_apr
from .features.volatility import sigma_price
from .features.vt_ratio import vt_ratio
from .models.il_model import expected_il_ref_from_closes


@dataclass(frozen=True)
class PoolDailyMetrics:
    """某个候选池在某一天的完整指标集（1.1-1.3 节单项指标，不含复合分——见模块说明）。"""

    chain: Chain
    pool_address: str
    as_of: date
    fee_apr: MetricValue[float]
    cake_apr: MetricValue[float]
    nominal_apr: MetricValue[float]
    vt_ratio: MetricValue[float]
    sigma_price: MetricValue[float]
    expected_il_ref: MetricValue[float]
    capital_volatility: MetricValue[float]
    age_penalty: MetricValue[float]
    depth_tier: MetricValue[DepthTier]


def compute_daily_metrics(
    *,
    chain: Chain,
    pool_address: str,
    fee_pips: int,
    created_at: datetime | None,
    as_of: date,
    history: list[PoolMetricsHistoryRow],
    fee_protocol: tuple[int, int] | None,
    cake_emission: tuple[float, float] | None,
    cake_usd_price: float | None,
) -> PoolDailyMetrics:
    """组装某个候选池在 `as_of` 这一天的完整指标集。

    @param history `pool_metrics_history` 里该池截止到 `as_of`（含）的全部快照行，按日期升序
        （用 `PoolMetricsRepository.get_series_up_to` 取得）
    @param fee_protocol 当前链上 `slot0()` 读到的 (feeProtocol0, feeProtocol1)；读不到传 None
    @param cake_emission 当前链上 `read_cake_emission` 的返回值；该池没有 CAKE farm 传 None
    @param cake_usd_price 当前 CAKE/USD 价格；取不到传 None
    """
    row_at_as_of = next((r for r in history if r.snapshot_date == as_of), None)
    tvl_at_as_of = row_at_as_of.tvl_usd if row_at_as_of else None
    volume_at_as_of = row_at_as_of.volume_24h_usd if row_at_as_of else None

    closes = [r.close_price for r in history if r.close_price is not None]
    tvls = [r.tvl_usd for r in history if r.tvl_usd is not None]

    as_of_end_of_day = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)

    if fee_protocol is None:
        fee = MetricValue.unavailable("链上 feeProtocol 读不到")
    else:
        fee = fee_apr(volume_at_as_of, tvl_at_as_of, fee_pips, fee_protocol[0], fee_protocol[1])
    cake = cake_apr(cake_emission, cake_usd_price, tvl_at_as_of)

    return PoolDailyMetrics(
        chain=chain,
        pool_address=pool_address,
        as_of=as_of,
        fee_apr=fee,
        cake_apr=cake,
        nominal_apr=nominal_apr(fee, cake),
        vt_ratio=vt_ratio(volume_at_as_of, tvl_at_as_of),
        sigma_price=sigma_price(closes),
        expected_il_ref=expected_il_ref_from_closes(closes),
        capital_volatility=capital_volatility(tvls),
        age_penalty=age_penalty(created_at, as_of_end_of_day),
        depth_tier=depth_tier(tvl_at_as_of),
    )
