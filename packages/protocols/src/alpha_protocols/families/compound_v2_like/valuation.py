"""Compound V2 系（Venus 等）的持仓估值（纯函数，规划 5.8）。

- share（存款，id 为市场）：读 `exchangeRateCurrent()`（静态调用，先计息）、`underlying()`，按持有人估值时再读
  `balanceOf(owner)`；底层 = vToken 数量 × 汇率 / 1e18，向下取整，与 `balanceOfUnderlying` 一致；
- debt（负债，id 为市场）：读 `borrowBalanceCurrent(owner)`（静态调用，先计息）、`underlying()`；符号为负；
- claimable（待领奖励，id 为 Comptroller）：读 `venusAccrued(owner)`。这只是已结算未领取的部分，最后一次结算
  之后新产生的不含在内，所以标为下限。

原生币市场（vBNB）没有 `underlying()`，底层资产由实例配置的 `native_market` 指定为原生币。
负债估值同时读 Comptroller 的 `getAccountLiquidity(owner)`，把 (错误码, 剩余借款额度, 缺口) 写进 extra。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from eth_abi import encode as abi_encode
from eth_utils import keccak

from ...decoding.models import NATIVE, PositionKind
from ...valuation.models import Component, ReadResults, StateRead, UnderlyingAmount, Valuation, ValuationRequest

EXCHANGE_RATE_SCALE = 10**18  # 汇率的定点精度（Exp.mantissa）


def _sel(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()[:8]


_EXCHANGE_RATE = _sel("exchangeRateCurrent()")
_BALANCE_OF = _sel("balanceOf(address)")
_UNDERLYING = _sel("underlying()")
_BORROW_BALANCE = _sel("borrowBalanceCurrent(address)")
_ACCRUED = _sel("venusAccrued(address)")
_LIQUIDITY = _sel("getAccountLiquidity(address)")


def _uint(raw: bytes, word: int = 0) -> int:
    return int.from_bytes(raw[32 * word : 32 * (word + 1)], "big")


@dataclass(frozen=True)
class CompoundV2Valuer:
    # 存款凭证（vToken 份额）、负债、待领奖励
    position_kinds: ClassVar[frozenset[PositionKind]] = frozenset(
        {PositionKind.SHARE, PositionKind.DEBT, PositionKind.CLAIMABLE}
    )
    instance_key: str
    comptroller: str
    native_market: str | None
    reward_token: str | None

    def plan(self, request: ValuationRequest, reads: ReadResults) -> list[StateRead]:
        pos, owner_arg = request.position, abi_encode(["address"], [request.position.owner]).hex()
        target = pos.id
        wanted: list[StateRead] = []
        if pos.kind in (PositionKind.SHARE, PositionKind.DEBT) and target != self.native_market:
            wanted.append(StateRead(f"{target}:underlying", target, _UNDERLYING))
        if pos.kind is PositionKind.SHARE:
            wanted.append(StateRead(f"{target}:exchangeRate", target, _EXCHANGE_RATE))
            if request.amount_raw is None:
                wanted.append(StateRead(f"{target}:balance:{pos.owner}", target, _BALANCE_OF + owner_arg))
        elif pos.kind is PositionKind.DEBT:
            wanted.append(StateRead(f"{target}:borrow:{pos.owner}", target, _BORROW_BALANCE + owner_arg))
            wanted.append(
                StateRead(f"{self.comptroller}:liquidity:{pos.owner}", self.comptroller, _LIQUIDITY + owner_arg)
            )
        elif pos.kind is PositionKind.CLAIMABLE:
            wanted.append(StateRead(f"{self.comptroller}:accrued:{pos.owner}", self.comptroller, _ACCRUED + owner_arg))
        return [r for r in wanted if r.key not in reads]

    def _underlying(self, market: str, reads: ReadResults) -> str | None:
        if market == self.native_market:
            return NATIVE
        raw = reads.get(f"{market}:underlying")
        return None if raw is None else "0x" + raw[12:32].hex()

    def unwrap(self, request: ValuationRequest, reads: ReadResults) -> Valuation:
        pos = request.position
        fail = Valuation(request, (), error=f"读取 {pos.key} 的状态失败")
        if pos.kind is PositionKind.CLAIMABLE:
            raw = reads.get(f"{self.comptroller}:accrued:{pos.owner}")
            if raw is None or self.reward_token is None:
                return fail
            return Valuation(
                request, (UnderlyingAmount(self.reward_token, _uint(raw), Component.REWARD, lower_bound=True),)
            )
        underlying = self._underlying(pos.id, reads)
        if underlying is None:
            return fail
        if pos.kind is PositionKind.SHARE:
            rate = reads.get(f"{pos.id}:exchangeRate")
            balance = request.amount_raw
            if balance is None:
                raw = reads.get(f"{pos.id}:balance:{pos.owner}")
                balance = None if raw is None else _uint(raw)
            if rate is None or balance is None:
                return fail
            amount = balance * _uint(rate) // EXCHANGE_RATE_SCALE
            return Valuation(
                request,
                (UnderlyingAmount(underlying, amount, Component.PRINCIPAL),),
                {"vtoken_amount": balance, "exchange_rate": _uint(rate)},
            )
        if pos.kind is PositionKind.DEBT:
            raw = reads.get(f"{pos.id}:borrow:{pos.owner}")
            if raw is None:
                return fail
            extra = {}
            liq = reads.get(f"{self.comptroller}:liquidity:{pos.owner}")
            if liq is not None:
                extra = {
                    "account_liquidity_error": _uint(liq, 0),
                    "liquidity": _uint(liq, 1),
                    "shortfall": _uint(liq, 2),
                }
            return Valuation(request, (UnderlyingAmount(underlying, _uint(raw), Component.DEBT, sign=-1),), extra)
        return Valuation(request, (), error=f"不支持的持仓形态 {pos.kind.value}")
