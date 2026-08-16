"""手续费年化 FeeAPR（pool-discovery-metrics-v1.md 1.1 节）。

    FeeAPR = (24h手续费收入 / TVL) × 365
    24h手续费收入 = 24h volume × feePips/1e6 × (1 − 平均协议抽成)

复用 alpha-lp `estimateFeeDailyUsd`（`packages/pancake-v3/src/range-estimate.ts:138`）的核心公式，
但取**全池维度**：alpha-lp 原实现按"用户份额"算（乘一个 `liquidityShare`），
池子发现场景不针对具体仓位，去掉这个份额因子——这是设计方案原文明确要求的处理方式。
"""

from __future__ import annotations

from alpha_core.metrics import MetricValue


def fee_apr(
    volume_24h_usd: float | None,
    tvl_usd: float | None,
    fee_pips: int,
    fee_protocol0: int,
    fee_protocol1: int,
) -> MetricValue[float]:
    """@param fee_protocol0/1 池子 `slot0()` 里的协议抽成，单位 1/10000（`read_fee_protocol` 的返回值）。"""
    if volume_24h_usd is None or tvl_usd is None:
        return MetricValue.unavailable("volume_24h_usd 或 tvl_usd 未知")
    if tvl_usd <= 0:
        return MetricValue.unavailable("TVL 非正数，FeeAPR 无意义")

    lp_net_share = 1 - (fee_protocol0 + fee_protocol1) / 2 / 10_000
    daily_fee_usd = volume_24h_usd * fee_pips / 1_000_000 * lp_net_share
    return MetricValue.available(daily_fee_usd / tvl_usd * 365)
