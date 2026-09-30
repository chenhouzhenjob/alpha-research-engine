"""Uniswap V3 系 NFT 仓位的估值（纯函数，规划 5.8）。

两轮读取：
1. NPM `positions(tokenId)`：token0、token1、fee、tick 区间、流动性、上次结算时的 feeGrowthInside、tokensOwed；
2. 池子（地址用 CREATE2 在本地算，不额外读）：`slot0()`、`feeGrowthGlobal0X128()`、`feeGrowthGlobal1X128()`、
   `ticks(tickLower)`、`ticks(tickUpper)`。

解包结果：token0、token1 各一份本金（取出全部流动性能拿回的数量）和未领手续费（`collect` 能领到的数量），
都与链上静态调用逐 wei 相等（见 tests/test_family_uniswap_v3_valuation.py）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak

from ...decoding.evm.rules import Create2Variant, create2_address
from ...decoding.models import PositionKind
from ...valuation.models import Component, ReadResults, StateRead, UnderlyingAmount, Valuation, ValuationRequest
from .math import amounts_for_liquidity, fee_growth_inside, fees_owed


def _sel(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()[:8]


_POSITIONS = _sel("positions(uint256)")
_SLOT0 = _sel("slot0()")
_FG0 = _sel("feeGrowthGlobal0X128()")
_FG1 = _sel("feeGrowthGlobal1X128()")
_TICKS = _sel("ticks(int24)")
# positions() 的返回：nonce、operator、token0、token1、fee、tickLower、tickUpper、liquidity、
# feeGrowthInside0LastX128、feeGrowthInside1LastX128、tokensOwed0、tokensOwed1
_POSITION_TYPES = ["uint96", "address", "address", "address", "uint24", "int24", "int24"]
_POSITION_TYPES += ["uint128", "uint256", "uint256", "uint128", "uint128"]


@dataclass(frozen=True)
class _Position:
    token0: str
    token1: str
    fee: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    inside0_last: int
    inside1_last: int
    owed0: int
    owed1: int


def _position(raw: bytes) -> _Position:
    _n, _op, t0, t1, fee, lo, hi, liq, fg0, fg1, o0, o1 = abi_decode(_POSITION_TYPES, raw)
    return _Position(t0.lower(), t1.lower(), fee, lo, hi, liq, fg0, fg1, o0, o1)


@dataclass(frozen=True)
class UniswapV3Valuer:
    position_kinds: ClassVar[frozenset[PositionKind]] = frozenset({PositionKind.NFT})  # 仓位 NFT
    instance_key: str
    position_manager: str
    pool_deployer: str  # 执行 CREATE2 的合约：PancakeSwap 为 pool_deployer，Uniswap 为 factory
    init_code_hash: str
    create2_variant: Create2Variant

    def _pool(self, p: _Position) -> str:
        salt = keccak(abi_encode(["address", "address", "uint24"], [p.token0, p.token1, p.fee]))
        return create2_address(self.create2_variant, self.pool_deployer, salt, self.init_code_hash)

    def plan(self, request: ValuationRequest, reads: ReadResults) -> list[StateRead]:
        tid = int(request.position.id)
        key = f"{tid}:positions"
        if key not in reads:
            return [StateRead(key, self.position_manager, _POSITIONS + abi_encode(["uint256"], [tid]).hex())]
        if reads[key] is None:
            return []  # 仓位不存在（已销毁）：unwrap 里报错
        p = _position(reads[key])
        pool = self._pool(p)
        tick_arg = lambda t: _TICKS + abi_encode(["int24"], [t]).hex()  # noqa: E731
        return [
            StateRead(f"{pool}:slot0", pool, _SLOT0),
            StateRead(f"{pool}:fg0", pool, _FG0),
            StateRead(f"{pool}:fg1", pool, _FG1),
            StateRead(f"{pool}:tick:{p.tick_lower}", pool, tick_arg(p.tick_lower)),
            StateRead(f"{pool}:tick:{p.tick_upper}", pool, tick_arg(p.tick_upper)),
        ]

    def unwrap(self, request: ValuationRequest, reads: ReadResults) -> Valuation:
        tid = int(request.position.id)
        raw = reads.get(f"{tid}:positions")
        if raw is None:
            return Valuation(request, (), error=f"读取仓位 {tid} 失败（可能已销毁）")
        p = _position(raw)
        pool = self._pool(p)
        needed = [
            f"{pool}:slot0",
            f"{pool}:fg0",
            f"{pool}:fg1",
            f"{pool}:tick:{p.tick_lower}",
            f"{pool}:tick:{p.tick_upper}",
        ]
        if any(reads.get(k) is None for k in needed):
            return Valuation(request, (), error=f"读取池子 {pool} 状态失败")
        slot0 = reads[f"{pool}:slot0"]
        sqrt_price = int.from_bytes(slot0[0:32], "big")
        tick = int.from_bytes(slot0[32:64], "big", signed=True)
        fg = [int.from_bytes(reads[f"{pool}:fg{i}"][:32], "big") for i in (0, 1)]
        lower, upper = reads[f"{pool}:tick:{p.tick_lower}"], reads[f"{pool}:tick:{p.tick_upper}"]
        # ticks() 返回 (liquidityGross, liquidityNet, feeGrowthOutside0X128, feeGrowthOutside1X128, …)
        outside = lambda raw, i: int.from_bytes(raw[(2 + i) * 32 : (3 + i) * 32], "big")  # noqa: E731
        a0, a1 = amounts_for_liquidity(tick, sqrt_price, p.tick_lower, p.tick_upper, p.liquidity)
        owed = []
        for i, (last, tokens_owed) in enumerate(((p.inside0_last, p.owed0), (p.inside1_last, p.owed1))):
            inside = fee_growth_inside(tick, p.tick_lower, p.tick_upper, fg[i], outside(lower, i), outside(upper, i))
            owed.append(fees_owed(tokens_owed, p.liquidity, inside, last))
        amounts = tuple(
            UnderlyingAmount(asset, amount, component)
            for asset, amount, component in (
                (p.token0, a0, Component.PRINCIPAL),
                (p.token1, a1, Component.PRINCIPAL),
                (p.token0, owed[0], Component.FEE),
                (p.token1, owed[1], Component.FEE),
            )
        )
        extra = {
            "pool": pool,
            "liquidity": p.liquidity,
            "tick": tick,
            "tick_lower": p.tick_lower,
            "tick_upper": p.tick_upper,
            "in_range": p.tick_lower <= tick < p.tick_upper,
        }
        return Valuation(request, amounts, extra)
