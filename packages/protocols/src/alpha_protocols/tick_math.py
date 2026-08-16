"""Uniswap V3 / PancakeSwap V3 的 sqrtPriceX96 ↔ 价格 ↔ tick 换算，阶段 1 新增
（`live-signal-system-设计方案.md` 第 5 章，`recommended_range` 需要落到具体 tick 范围）。

全代码库此前没有"价格转 tick"的反方向数学——只有 `lp-backtest/aggregate_candles.py` 私有的
`_price_from_sqrt`（sqrt price 转价格，正方向）。这里把它收敛成共享实现（公式完全一致，
不是重新推导），并补上反方向（价格转 tick）。
"""

from __future__ import annotations

import math

TICK_BASE = 1.0001


def price_from_sqrt_price_x96(sqrt_price_x96: int, decimals0: int, decimals1: int) -> float:
    """@returns token1/token0 的人类可读价格（已按两个 token 的 decimals 调整）。

    跟 `aggregate_candles.py::_price_from_sqrt` 同一个公式：
    `raw_ratio = (sqrtPriceX96 / 2^96)^2` 是链上原始（未调整精度）的价格比，
    再乘 `10^(decimals0-decimals1)` 调整成人类可读价格。
    """
    raw_ratio = (sqrt_price_x96 / 2**96) ** 2
    return raw_ratio * 10 ** (decimals0 - decimals1)


def price_to_tick(price: float, decimals0: int, decimals1: int) -> int:
    """`price_from_sqrt_price_x96` 的反函数。

    先还原出未调整精度的原始价格比 `raw_ratio = price / 10^(decimals0-decimals1)`
    （Uniswap V3 的 tick 定义在原始价格比上，不是人类可读价格），再取以 1.0001 为底的对数。
    """
    raw_ratio = price / 10 ** (decimals0 - decimals1)
    return round(math.log(raw_ratio) / math.log(TICK_BASE))


def round_to_tick_spacing(tick: int, tick_spacing: int) -> int:
    """把任意 tick 舍入到最近的合法 tick（必须是 `tick_spacing` 的整数倍才能作为仓位边界）。"""
    return round(tick / tick_spacing) * tick_spacing
