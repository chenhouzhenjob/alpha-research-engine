"""Uniswap V2 系 LP 份额的估值（纯函数，规划 5.8）。

按交易对 `burn` 的算法：先按 `_mintFee` 算出协议费要增发的 LP（稀释所有持有人），再按份额从交易对
持有的 token 余额里拆出底层资产。与链上真实 Burn 事件逐 wei 相等（见 tests/test_family_uniswap_v2.py）。

    L_fee = ts·(rootK − rootKLast)·n / (rootK·d + rootKLast·n)    （feeTo ≠ 0、kLast ≠ 0、rootK > rootKLast 时）
    amount_i = liquidity · balance_i / (ts + L_fee)

`n/d` 是实例配置的协议费常数：Uniswap 为 1/5，PancakeSwap 为 8/17（均已对照合约源码核实）。
rootK 用储备（getReserves）算，取回数量用交易对实际持有的 token 余额（balanceOf）算，与合约一致。

两轮读取：
1. 交易对的 `token0()`、`token1()`，以及（按持有人估值时）持有人的 LP 余额；
2. `getReserves()`、`totalSupply()`、`kLast()`、工厂的 `feeTo()`、交易对持有的 token0 / token1 余额。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isqrt

from eth_abi import encode as abi_encode
from eth_utils import keccak

from ...valuation.models import Component, ReadResults, StateRead, UnderlyingAmount, Valuation, ValuationRequest


def _sel(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()[:8]


_TOKEN0, _TOKEN1 = _sel("token0()"), _sel("token1()")
_RESERVES, _TOTAL_SUPPLY, _KLAST = _sel("getReserves()"), _sel("totalSupply()"), _sel("kLast()")
_FEE_TO, _BALANCE_OF = _sel("feeTo()"), _sel("balanceOf(address)")


def _uint(raw: bytes, word: int = 0) -> int:
    return int.from_bytes(raw[32 * word : 32 * (word + 1)], "big")


def _addr(raw: bytes) -> str:
    return "0x" + raw[12:32].hex()


def protocol_fee_liquidity(total_supply: int, reserve0: int, reserve1: int, k_last: int, n: int, d: int) -> int:
    """`_mintFee` 要增发给 feeTo 的 LP 数量（调用方已确认 feeTo ≠ 0）。"""
    if k_last == 0:
        return 0
    root_k, root_k_last = isqrt(reserve0 * reserve1), isqrt(k_last)
    if root_k <= root_k_last:
        return 0
    return total_supply * (root_k - root_k_last) * n // (root_k * d + root_k_last * n)


@dataclass(frozen=True)
class UniswapV2Valuer:
    instance_key: str
    factory: str
    fee_numerator: int
    fee_denominator: int

    def plan(self, request: ValuationRequest, reads: ReadResults) -> list[StateRead]:
        pair, owner = request.position.id, request.position.owner
        first = [StateRead(f"{pair}:token0", pair, _TOKEN0), StateRead(f"{pair}:token1", pair, _TOKEN1)]
        if request.amount_raw is None:
            first.append(StateRead(f"{pair}:lp:{owner}", pair, _BALANCE_OF + abi_encode(["address"], [owner]).hex()))
        if any(r.key not in reads for r in first):
            return first
        if reads[f"{pair}:token0"] is None or reads[f"{pair}:token1"] is None:
            return []
        t0, t1 = _addr(reads[f"{pair}:token0"]), _addr(reads[f"{pair}:token1"])
        of_pair = abi_encode(["address"], [pair]).hex()
        return [
            StateRead(f"{pair}:reserves", pair, _RESERVES),
            StateRead(f"{pair}:totalSupply", pair, _TOTAL_SUPPLY),
            StateRead(f"{pair}:kLast", pair, _KLAST),
            StateRead(f"{self.factory}:feeTo", self.factory, _FEE_TO),
            StateRead(f"{pair}:balance:{t0}", t0, _BALANCE_OF + of_pair),
            StateRead(f"{pair}:balance:{t1}", t1, _BALANCE_OF + of_pair),
        ]

    def unwrap(self, request: ValuationRequest, reads: ReadResults) -> Valuation:
        pair, owner = request.position.id, request.position.owner
        try:
            t0, t1 = _addr(reads[f"{pair}:token0"]), _addr(reads[f"{pair}:token1"])
            reserves, total_supply = reads[f"{pair}:reserves"], _uint(reads[f"{pair}:totalSupply"])
            k_last, fee_to = _uint(reads[f"{pair}:kLast"]), _addr(reads[f"{self.factory}:feeTo"])
            balances = [_uint(reads[f"{pair}:balance:{t}"]) for t in (t0, t1)]
            liquidity = request.amount_raw if request.amount_raw is not None else _uint(reads[f"{pair}:lp:{owner}"])
        except (KeyError, TypeError):
            return Valuation(request, (), error=f"读取交易对 {pair} 的状态失败")
        fee_on = fee_to != "0x" + "0" * 40
        minted = (
            protocol_fee_liquidity(
                total_supply, _uint(reserves, 0), _uint(reserves, 1), k_last, self.fee_numerator, self.fee_denominator
            )
            if fee_on
            else 0
        )
        supply = total_supply + minted
        amounts = tuple(
            UnderlyingAmount(token, liquidity * balance // supply if supply else 0, Component.PRINCIPAL)
            for token, balance in zip((t0, t1), balances, strict=True)
        )
        return Valuation(
            request, amounts, {"liquidity": liquidity, "total_supply": total_supply, "protocol_fee_liquidity": minted}
        )
