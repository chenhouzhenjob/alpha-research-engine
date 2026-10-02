"""Uniswap V2 系的钱包视角解码：通过路由添加 / 移除流动性、交换（纯函数，规划 5.8）。

路由是交易的 `to` 时本实例参与分派；按调用方法分成三类（`calls.py`），交易对的事件和转账从整张回执读。
路由只和自己工厂的交易对交互（路由内部用 CREATE2 算交易对地址），所以路由调用里出现的交易对都属于本实例。
钱包绕过路由直接和交易对交互的情况，等识别第一层把交易对登记进 contract_registry 后再覆盖（步骤 12）。

| 操作 | 标准事件 |
|---|---|
| 添加流动性 | 付给交易对的每个 token：`deposit/deposit_asset`（out），付原生币时认领交易 value；
|   | 收到的 LP：`receive/receive_wrapped`（in）；多付的原生币退款：`withdrawal/remove_asset`（extra.refund） |
| 移除流动性 | 交出的 LP：`spend/return_wrapped`（out）；收到的 token 和原生币：`withdrawal/remove_asset`（in） |
| 交换 | 按资产算钱包净额：净流出 `trade/spend`，净流入 `trade/receive`；多付的原生币退款同上 |

原生币推断（规划 5.5，已用样本逐 wei 核对）：
- 路由解包后转给 `to` 的原生币 = 路由的 `Withdrawal`：移除流动性取最后一次（先解除流动性、再把 token 转给用户时
  可能触发转账税的卖出，最后才 withdraw）；交换取钱包转出 token 之后的第一次（带转账税的 token 会在转账过程中
  自己通过路由卖税，产生更早的解包）；
- 多付的原生币退款 = 交易 value − 路由包装的数量（`Deposit(dst=路由)` 之和）。
"""

from __future__ import annotations

from dataclasses import dataclass

from alpha_core.chain_data import RawLog

from ...decoding.consolidate import net_by_asset, trade_from_net
from ...decoding.evm.dispatch import FamilyRun
from ...decoding.evm.flows import event_topic
from ...decoding.models import (
    NATIVE,
    AssetFlowKind,
    Direction,
    EventSubtype,
    EventType,
    FlowSource,
    PositionKind,
    PositionRef,
    RiskFlag,
    WarningCode,
)
from .calls import Op, RouterCall, parse_router_call

PAIR_MINT = event_topic("Mint(address,uint256,uint256)")
PAIR_BURN = event_topic("Burn(address,uint256,uint256,address)")
PAIR_SWAP = event_topic("Swap(address,uint256,uint256,uint256,uint256,address)")
PAIR_SYNC = event_topic("Sync(uint112,uint112)")
DEPOSIT = event_topic("Deposit(address,uint256)")
WITHDRAWAL = event_topic("Withdrawal(address,uint256)")
_PAIR_TOPICS = frozenset({PAIR_MINT, PAIR_BURN, PAIR_SWAP, PAIR_SYNC})


def _addr(topic: str) -> str:
    return "0x" + topic[-40:]


def _word0(data: str) -> int:
    return int(data[2:66], 16)


@dataclass(frozen=True)
class UniswapV2Decoder:
    family: str
    instance_key: str
    decoder_version: str
    routers: frozenset[str]

    def decode(self, run: FamilyRun) -> None:
        if not run.is_call_target or run.tx.to_address not in self.routers:
            return
        call = parse_router_call(run.tx.input)
        if call is None:
            return
        logs = sorted(run.receipt.logs, key=lambda lg: lg.log_index)
        pairs = {lg.address for lg in logs if lg.topics and lg.topics[0] in _PAIR_TOPICS}
        router = run.tx.to_address
        if call.method.refunds_eth:
            self._refund(run, logs, router)
        if call.method.op is Op.ADD:
            self._add(run, pairs, router)
        elif call.method.op is Op.REMOVE:
            self._remove(run, call, logs, pairs, router)
        else:
            self._swap(run, call, logs, router)

    # ------------------------------------------------------------------

    def _key(self, run: FamilyRun, pair: str) -> str:
        return PositionRef(run.ctx.chain, self.instance_key, PositionKind.SHARE, pair, run.subject).key

    def _wrapped(self, run: FamilyRun, logs: list[RawLog], topic0: str, router: str) -> list[RawLog]:
        return [
            lg
            for lg in logs
            if lg.address == run.ctx.wrapped_native and lg.topics[:1] == [topic0] and _addr(lg.topics[1]) == router
        ]

    def _claim_each(
        self, run: FamilyRun, flows, event_type, subtype, direction, *, key=None, counterparty=None
    ) -> None:
        for f in flows:
            run.claim_event(
                event_type, subtype, direction, [f.flow_id], position_key=key, counterparty=counterparty or f.to_address
            )

    def _refund(self, run: FamilyRun, logs: list[RawLog], router: str) -> None:
        if run.tx.from_address != run.subject or not run.tx.value:
            return
        wrapped = sum(_word0(lg.data) for lg in self._wrapped(run, logs, DEPOSIT, router))
        refund = run.tx.value - wrapped
        if refund <= 0:
            return
        evidence = self._wrapped(run, logs, DEPOSIT, router)[-1:] or logs[-1:]
        if not evidence:
            return
        fid = run.infer(AssetFlowKind.NATIVE, NATIVE, refund, router, run.subject, evidence=evidence[0])
        if fid is not None:
            run.claim_event(
                EventType.WITHDRAWAL,
                EventSubtype.REMOVE_ASSET,
                Direction.IN,
                [fid],
                counterparty=router,
                extra={"refund": True},
            )

    def _native_out(self, run: FamilyRun, call: RouterCall, evidence: RawLog | None) -> None:
        """推断路由解包后转给钱包的原生币（只在 `to` 是钱包时）。"""
        if not call.method.eth_out or call.to != run.subject:
            return
        if evidence is None:
            run.warn(WarningCode.INTERNAL_UNAVAILABLE, "路由应当解包 WETH 转出原生币，但找不到 Withdrawal")
            return
        router = _addr(evidence.topics[1])  # Withdrawal(src=路由)：路由解包后把原生币转给 to
        run.infer(AssetFlowKind.NATIVE, NATIVE, _word0(evidence.data), router, run.subject, evidence=evidence)

    def _add(self, run: FamilyRun, pairs: set[str], router: str) -> None:
        for pair in sorted(pairs):
            paid = run.ledger.find(
                lambda f, pair=pair: (
                    f.kind is AssetFlowKind.ERC20 and f.from_address == run.subject and f.to_address == pair
                )
            )
            lp = run.ledger.find(
                lambda f, pair=pair: f.kind is AssetFlowKind.ERC20 and f.asset == pair and f.to_address == run.subject
            )
            if not lp:
                continue
            key = self._key(run, pair)
            self._claim_each(run, paid, EventType.DEPOSIT, EventSubtype.DEPOSIT_ASSET, Direction.OUT, key=key)
            value = run.ledger.find(
                lambda f: f.kind is AssetFlowKind.NATIVE and f.source is FlowSource.TX and f.from_address == run.subject
            )
            self._claim_each(
                run, value, EventType.DEPOSIT, EventSubtype.DEPOSIT_ASSET, Direction.OUT, key=key, counterparty=router
            )
            self._claim_each(
                run, lp, EventType.RECEIVE, EventSubtype.RECEIVE_WRAPPED, Direction.IN, key=key, counterparty=pair
            )

    def _remove(self, run: FamilyRun, call: RouterCall, logs: list[RawLog], pairs: set[str], router: str) -> None:
        withdrawals = self._wrapped(run, logs, WITHDRAWAL, router)
        self._native_out(run, call, withdrawals[-1] if withdrawals else None)
        for pair in sorted(pairs):
            lp_out = run.ledger.find(
                lambda f, pair=pair: f.kind is AssetFlowKind.ERC20 and f.asset == pair and f.from_address == run.subject
            )
            if not lp_out:
                continue
            key = self._key(run, pair)
            self._claim_each(
                run, lp_out, EventType.SPEND, EventSubtype.RETURN_WRAPPED, Direction.OUT, key=key, counterparty=pair
            )
            received = run.ledger.find(
                lambda f, pair=pair: (
                    f.to_address == run.subject
                    and f.from_address in (pair, router)
                    and f.kind in (AssetFlowKind.ERC20, AssetFlowKind.NATIVE)
                )
            )
            for f in received:
                run.claim_event(
                    EventType.WITHDRAWAL,
                    EventSubtype.REMOVE_ASSET,
                    Direction.IN,
                    [f.flow_id],
                    position_key=key,
                    counterparty=pair,
                )

    def _swap(self, run: FamilyRun, call: RouterCall, logs: list[RawLog], router: str) -> None:
        withdrawals = self._wrapped(run, logs, WITHDRAWAL, router)
        first_out = min(
            (
                f.log_index
                for f in run.ledger.flows
                if f.from_address == run.subject and f.kind is AssetFlowKind.ERC20 and f.log_index is not None
            ),
            default=-1,
        )
        self._native_out(run, call, next((lg for lg in withdrawals if lg.log_index > first_out), None))
        flows = [
            f
            for f in run.ledger.unclaimed()
            if f.kind is not AssetFlowKind.GAS and run.ctx.risk_of(f.asset) in (RiskFlag.NORMAL, RiskFlag.HACKED)
        ]
        legs = trade_from_net(net_by_asset(flows, run.subject))
        by_id = {f.flow_id: f for f in flows}
        for positions, subtype, outgoing in (
            (legs.spend, EventSubtype.SPEND, True),
            (legs.receive, EventSubtype.RECEIVE, False),
        ):
            for p in positions:
                # 只认领净方向上的流水；反方向的（例如找零）留给兜底
                ids = [i for i in p.flow_ids if (by_id[i].from_address == run.subject) == outgoing]
                if ids:
                    run.claim_event(
                        EventType.TRADE, subtype, Direction.OUT if outgoing else Direction.IN, ids, counterparty=router
                    )
