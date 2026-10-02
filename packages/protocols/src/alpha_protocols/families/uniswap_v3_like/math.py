"""Uniswap V3 的整数数学（纯函数），逐行移植合约，结果与链上逐 wei 相等。

- `get_sqrt_ratio_at_tick`：`TickMath.getSqrtRatioAtTick`，按位乘常数表，最后向上取整到 Q64.96；
- `amounts_for_liquidity`：池子 `_modifyPosition` 取出流动性时的本金，用 `SqrtPriceMath.getAmount{0,1}Delta`
  （向下取整）；是否在区间内按池子的判断用 `tick`，不用 sqrtPrice；
- `fee_growth_inside` / `fees_owed`：`Tick.getFeeGrowthInside` 与 `Position.update`，差值按 2²⁵⁶ 取模
  （合约里是 unchecked 溢出语义）。

`alpha_protocols/tick_math.py` 是 live-signal 用的浮点近似，不能用于估值。
"""

from __future__ import annotations

MIN_TICK = -887272
MAX_TICK = 887272
Q96 = 1 << 96
Q128 = 1 << 128
_MOD256 = 1 << 256

# TickMath.getSqrtRatioAtTick 的常数表：第 i 位为 1 时乘上 1/sqrt(1.0001)^(2^i)（Q128）
_BITS = (
    (0x2, 0xFFF97272373D413259A46990580E213A),
    (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC),
    (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
    (0x10, 0xFFCB9843D60F6159C9DB58835C926644),
    (0x20, 0xFF973B41FA98C081472E6896DFB254C0),
    (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
    (0x80, 0xFE5DEE046A99A2A811C461F1969C3053),
    (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4),
    (0x200, 0xF987A7253AC413176F2B074CF7815E54),
    (0x400, 0xF3392B0822B70005940C7A398E4B70F3),
    (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9),
    (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
    (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5),
    (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7),
    (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
    (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9),
    (0x20000, 0x5D6AF8DEDB81196699C329225EE604),
    (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
    (0x80000, 0x48A170391F7DC42444E8FA2),
)


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """tick 对应的 sqrt(1.0001^tick)，Q64.96 定点。

    @raises ValueError tick 超出 [MIN_TICK, MAX_TICK]
    """
    abs_tick = abs(tick)
    if abs_tick > MAX_TICK:
        raise ValueError(f"tick 超出范围：{tick}")
    ratio = 0xFFFCB933BD6FAD37AA2D162D1A594001 if abs_tick & 0x1 else 0x100000000000000000000000000000000
    for bit, factor in _BITS:
        if abs_tick & bit:
            ratio = (ratio * factor) >> 128
    if tick > 0:
        ratio = (_MOD256 - 1) // ratio
    return (ratio >> 32) + (0 if ratio % (1 << 32) == 0 else 1)


def mul_div(a: int, b: int, denominator: int) -> int:
    """FullMath.mulDiv：a×b÷denominator 向下取整（Python 整数不会溢出，直接算）。"""
    return a * b // denominator


def amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    """SqrtPriceMath.getAmount0Delta（向下取整）。"""
    lo, hi = sorted((sqrt_a, sqrt_b))
    return mul_div(liquidity << 96, hi - lo, hi) // lo


def amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    """SqrtPriceMath.getAmount1Delta（向下取整）。"""
    lo, hi = sorted((sqrt_a, sqrt_b))
    return mul_div(liquidity, hi - lo, Q96)


def amounts_for_liquidity(
    tick: int, sqrt_price_x96: int, tick_lower: int, tick_upper: int, liquidity: int
) -> tuple[int, int]:
    """取出全部流动性时能拿回的本金 (amount0, amount1)，与池子 burn 的结果一致。"""
    sa, sb = get_sqrt_ratio_at_tick(tick_lower), get_sqrt_ratio_at_tick(tick_upper)
    if tick < tick_lower:
        return amount0_delta(sa, sb, liquidity), 0
    if tick < tick_upper:
        return amount0_delta(sqrt_price_x96, sb, liquidity), amount1_delta(sa, sqrt_price_x96, liquidity)
    return 0, amount1_delta(sa, sb, liquidity)


def fee_growth_inside(
    tick: int, tick_lower: int, tick_upper: int, global_x128: int, outside_lower_x128: int, outside_upper_x128: int
) -> int:
    """Tick.getFeeGrowthInside（单个 token），结果按 2²⁵⁶ 取模。"""
    below = outside_lower_x128 if tick >= tick_lower else (global_x128 - outside_lower_x128) % _MOD256
    above = outside_upper_x128 if tick < tick_upper else (global_x128 - outside_upper_x128) % _MOD256
    return (global_x128 - below - above) % _MOD256


def fees_owed(tokens_owed: int, liquidity: int, inside_x128: int, inside_last_x128: int) -> int:
    """仓位当前可领取的数量 = 已记账的 tokensOwed + 上次更新以来新增的手续费（Position.update）。"""
    return tokens_owed + mul_div((inside_x128 - inside_last_x128) % _MOD256, liquidity, Q128)
