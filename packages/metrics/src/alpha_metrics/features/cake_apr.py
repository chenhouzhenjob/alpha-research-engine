"""CAKE 激励年化 CakeAPR（pool-discovery-metrics-v1.md 1.1 节）。

    CakeAPR = (cakePerSecond × 86400 × lmLiquidityShare × CAKE_USD价格 / TVL) × 365

数据源对应 alpha-lp `estimateCakeDaily`（`packages/pancake-v3/src/range-estimate.ts:160`），
`lmLiquidityShare` 定义见 `PancakeswapV3Plugin.read_cake_emission` 的 docstring
（池子当前活跃流动性里已质押进 MasterChef 的比例，不是单个仓位的份额）。

**注意衰减**：CAKE emission 会周期性调整（governance 投票），这里的 `cakePerSecond` 只是链上当前快照值。
经调研（见 apps/lp-backtest/README.md），alpha-lp 现有实现和 MasterChef V3 合约都只读当前状态，
没有历史 emission 事件可以重建，所以本函数算出来的是"基于当前 emission rate 静态外推"的年化值，
不代表未来 365 天不变——这是设计方案已知局限，不是本实现遗漏。
"""

from __future__ import annotations

from alpha_core.metrics import MetricValue


def cake_apr(
    cake_emission: tuple[float, float] | None,
    cake_usd_price: float | None,
    tvl_usd: float | None,
) -> MetricValue[float]:
    """@param cake_emission `(cakePerSecond, lmLiquidityShare)`；`None` 表示该池当前没有 CAKE farm。"""
    if cake_emission is None:
        return MetricValue.no_incentive("该池当前没有 CAKE 挖矿激励（或排放已结束）")
    if cake_usd_price is None:
        return MetricValue.unavailable("CAKE/USD 价格不可得")
    if tvl_usd is None or tvl_usd <= 0:
        return MetricValue.unavailable("TVL 未知或非正数")

    cake_per_second, lm_liquidity_share = cake_emission
    daily_cake_usd = cake_per_second * 86_400 * lm_liquidity_share * cake_usd_price
    return MetricValue.available(daily_cake_usd / tvl_usd * 365)
