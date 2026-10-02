"""包装原生币的解码（纯函数）。

WETH9 实现（WBNB、以太坊和 Base 的 WETH）的 `Deposit`、`Withdrawal` **不发出 `Transfer` 日志**：
- 包装 `Deposit(dst, wad)`：`dst` 付出 `wad` 原生币（通常就是交易 value），收到 `wad` 包装币（没有日志，推断）；
- 解包 `Withdrawal(src, wad)`：`src` 交出 `wad` 包装币、收到 `wad` 原生币，两边都没有日志，都要推断。

事件按"资产 ↔ 凭证"的对称写法（规划 5.3）：包装 = `deposit/deposit_asset`（原生币出）+
`receive/receive_wrapped`（包装币入）；解包 = `spend/return_wrapped` + `withdrawal/remove_asset`。

只处理钱包自己是 dst / src 的情况。路由、NPM 等合约替钱包包装或解包时，`dst`/`src` 是那个合约，
原生币怎么到钱包由对应家族推断（规划 5.5）。

推断之前先找有没有对得上的真实流水（交易 value，或某些实现本身也发了 Transfer），有就认领它，
避免重复计算。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...decoding.evm.dispatch import FamilyRun
from ...decoding.evm.flows import event_topic
from ...decoding.models import (
    NATIVE,
    AssetFlowKind,
    Direction,
    EventSubtype,
    EventType,
    FlowSource,
    WarningCode,
)

DEPOSIT = event_topic("Deposit(address,uint256)")
WITHDRAWAL = event_topic("Withdrawal(address,uint256)")
_ZERO = "0x" + "0" * 40


def _addr(topic: str) -> str:
    return "0x" + topic[-40:]


def _existing(run: FamilyRun, kind: AssetFlowKind, asset: str, amount: int, froms, tos) -> list:
    """未认领、非推断、数量和两端都对得上的流水。"""
    return run.ledger.find(
        lambda f: (
            f.kind is kind
            and f.asset == asset
            and f.amount_raw == amount
            and f.from_address in froms
            and f.to_address in tos
            and f.source is not FlowSource.INFERRED
        )
    )


def _find_or_infer(run: FamilyRun, log, kind: AssetFlowKind, asset: str, amount: int, froms, tos) -> int | None:
    """有对得上的真实流水就用它，否则推断一条（付款方、收款方取候选里的最后一个）。"""
    existing = _existing(run, kind, asset, amount, froms, tos)
    if existing:
        return existing[0].flow_id
    return run.infer(kind, asset, amount, froms[-1], tos[-1], evidence=log)


@dataclass(frozen=True)
class WrappedNativeDecoder:
    family: str
    instance_key: str
    decoder_version: str

    def decode(self, run: FamilyRun) -> None:
        for log in run.logs:
            if len(log.topics) != 2 or len(log.data) < 66:
                continue
            party, wad = _addr(log.topics[1]), int(log.data[2:66], 16)
            if party != run.subject:
                continue
            if log.topics[0] == DEPOSIT:
                self._deposit(run, log, wad)
            elif log.topics[0] == WITHDRAWAL:
                self._withdrawal(run, log, wad)

    def _deposit(self, run: FamilyRun, log, wad: int) -> None:
        wrapped, me = log.address, (run.subject,)
        paid_by_value = bool(_existing(run, AssetFlowKind.NATIVE, NATIVE, wad, me, (wrapped,)))
        native = _find_or_infer(run, log, AssetFlowKind.NATIVE, NATIVE, wad, me, (wrapped,))
        if not paid_by_value and not run.ledger.internal_available:
            # 包装用的原生币不是交易 value：钱包（通常是合约）是通过内部调用拿到这笔钱的。付出是确定的事实，
            # 照常推断；但收到它的那笔内部转移不可见，余额会对不上，必须告警
            run.warn(WarningCode.INTERNAL_UNAVAILABLE, f"日志 {log.log_index} 包装的原生币来自不可见的内部调用")
        token = _find_or_infer(run, log, AssetFlowKind.ERC20, wrapped, wad, (_ZERO, wrapped), me)
        if native is not None:
            run.claim_event(
                EventType.DEPOSIT, EventSubtype.DEPOSIT_ASSET, Direction.OUT, [native], counterparty=wrapped
            )
        if token is not None:
            run.claim_event(
                EventType.RECEIVE, EventSubtype.RECEIVE_WRAPPED, Direction.IN, [token], counterparty=wrapped
            )

    def _withdrawal(self, run: FamilyRun, log, wad: int) -> None:
        wrapped, me = log.address, (run.subject,)
        token = _find_or_infer(run, log, AssetFlowKind.ERC20, wrapped, wad, me, (_ZERO, wrapped))
        native = _find_or_infer(run, log, AssetFlowKind.NATIVE, NATIVE, wad, (wrapped,), me)
        if token is not None:
            run.claim_event(EventType.SPEND, EventSubtype.RETURN_WRAPPED, Direction.OUT, [token], counterparty=wrapped)
        if native is not None:
            run.claim_event(
                EventType.WITHDRAWAL, EventSubtype.REMOVE_ASSET, Direction.IN, [native], counterparty=wrapped
            )
