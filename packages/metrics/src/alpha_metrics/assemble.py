"""把"读链上当前状态 + 调用 compute_daily_metrics"这套编排逻辑抽成一个函数，避免
`lp_backtest.metrics`（CLI）、`lp_backtest.calibrate.weights`、`live-signal` 的 `/conclusion`
端点三处各写一遍同样的"读 feeProtocol/CAKE 排放 + 拼指标"逻辑——这是真实的三处重复，不是提前抽象。
"""

from __future__ import annotations

from datetime import date, datetime

from alpha_chains.base import ChainAdapter
from alpha_core.types import Chain
from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin
from alpha_storage.models import PoolMetricsHistoryRow

from .chain_reads import read_cake_emission_safe, read_fee_protocol_safe
from .compute import PoolDailyMetrics, compute_daily_metrics


def assemble_daily_metrics(
    *,
    chain: Chain,
    pool_address: str,
    fee_pips: int,
    created_at: datetime | None,
    as_of: date,
    history: list[PoolMetricsHistoryRow],
    adapter: ChainAdapter,
    plugin: PancakeswapV3Plugin,
    cake_usd_price: float | None,
) -> PoolDailyMetrics:
    """组装某个候选池在 `as_of` 这一天的完整指标集：先读链上当前状态（feeProtocol/CAKE 排放），
    再调用 `compute_daily_metrics`。

    `cake_usd_price` 由调用方传入，这里不自己查——同一批次要算多个池子时，CAKE 价格只需要查一次，
    不该每个池子各查一次浪费一次外部请求。
    """
    fee_protocol = read_fee_protocol_safe(adapter, plugin, pool_address)
    cake_emission = read_cake_emission_safe(adapter, plugin, pool_address)
    return compute_daily_metrics(
        chain=chain,
        pool_address=pool_address,
        fee_pips=fee_pips,
        created_at=created_at,
        as_of=as_of,
        history=history,
        fee_protocol=fee_protocol,
        cake_emission=cake_emission,
        cake_usd_price=cake_usd_price,
    )
