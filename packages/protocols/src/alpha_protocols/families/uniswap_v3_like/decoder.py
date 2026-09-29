"""Uniswap V3 系的钱包视角解码：NonfungiblePositionManager（NPM）仓位的开仓、增减流动性、领取（纯函数）。

只有 NPM 发出的日志分派到这里；池子的 Mint / Burn / Collect 和 token 的 Transfer 从整张回执里读，
用来补上池子地址、tick 区间和每一边是哪个 token（NPM 事件里只有数量，没有 token 地址）。
池子事件按"owner 是本实例的 NPM + 数量相同"与 NPM 事件配对，PancakeSwap 和 Uniswap 的签名一致。

事件（规划 5.8）：

| 链上动作 | 标准事件 |
|---|---|
| 仓位 NFT 从零地址转给钱包 | `mint/none`（in），extra 带池子、tick 区间 |
| `IncreaseLiquidity` | 钱包付给池子的每个 token：`deposit/deposit_asset`（out）；付原生币时认领交易 value |
| 付原生币时多付的部分（`refundETH`） | 推断 NPM 退回的原生币：`withdrawal/remove_asset`（in），extra.refund |
| `DecreaseLiquidity` + 同一笔交易的 `Collect` | 每个 token 拆成本金 `withdrawal/remove_asset` + 手续费 `claim/lp_fee`，
|   | 按 `fee = collect − decrease` 拆分流水 |
| 只有 `Collect` | `claim/lp_fee`，extra.split_deferred（可能含更早交易里挂起的本金，由 M3 结转） |
| `collect − decrease < 0` | 本金是更早的交易里挂起的：整笔 `withdrawal/remove_asset`，extra.split_deferred |
| 只有 `DecreaseLiquidity` | 本金留在 NPM 里挂起，没有资产流动：状态事件 `informational/none` |
| 仓位 NFT 转给零地址 / 在钱包之间转移 | `burn/none` / `transfer/none`，都带持仓键 |

`collect` 的收款方写成 NPM 自己时，钱随后由 `unwrapWETH9`（换成原生币，推断）或 `sweepToken` 转出，
收款方从调用数据里读（见 `calls.py`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alpha_core.chain_data import RawLog

from ...decoding.evm.dispatch import FamilyRun
from ...decoding.evm.flows import TRANSFER, event_topic
from ...decoding.models import (
    NATIVE,
    AssetFlowKind,
    Direction,
    EventSubtype,
    EventType,
    FlowSource,
    PositionKind,
    PositionRef,
    WarningCode,
)
from .calls import NpmCalls, parse_npm_calls

INCREASE = event_topic("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
DECREASE = event_topic("DecreaseLiquidity(uint256,uint128,uint256,uint256)")
NPM_COLLECT = event_topic("Collect(uint256,address,uint256,uint256)")
POOL_MINT = event_topic("Mint(address,address,int24,int24,uint128,uint256,uint256)")
POOL_BURN = event_topic("Burn(address,int24,int24,uint128,uint256,uint256)")
POOL_COLLECT = event_topic("Collect(address,address,int24,int24,uint128,uint128)")
DEPOSIT = event_topic("Deposit(address,uint256)")
WITHDRAWAL = event_topic("Withdrawal(address,uint256)")
_ZERO = "0x" + "0" * 40


def _words(data: str) -> list[int]:
    raw = data.removeprefix("0x")
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def _addr(value: str | int) -> str:
    text = value if isinstance(value, str) else format(value, "064x")
    return "0x" + text[-40:]


def _int24(topic: str) -> int:
    v = int(topic, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


@dataclass
class _Tx:
    """一笔交易里解码要用的上下文，按日志序号排好。"""

    run: FamilyRun
    npm: frozenset[str]
    calls: NpmCalls
    logs: list[RawLog]
    used: set[int] = field(default_factory=set)  # 已经配对过的池子日志、token 转账日志

    def pool_event(self, topic0: str, amounts: tuple[int, int], before: int, data_offset: int) -> RawLog | None:
        """owner 是本实例 NPM、数量对得上的池子事件（取最近的一条）。

        @param data_offset 数量在 data 里从第几个字开始（Mint、Burn、Collect 的字段顺序不同）
        """
        for lg in reversed([lg for lg in self.logs if lg.log_index < before]):
            if lg.log_index in self.used or not lg.topics or lg.topics[0] != topic0 or len(lg.topics) != 4:
                continue
            w = _words(lg.data)
            if _addr(lg.topics[1]) in self.npm and tuple(w[data_offset : data_offset + 2]) == amounts:
                self.used.add(lg.log_index)
                return lg
        return None

    def token_of(self, amount: int, *, frm: str | None, to: str | None, before: int) -> str | None:
        """在 `before` 之前找一条数量对得上的 ERC20 转账，返回 token 地址。"""
        for lg in reversed([lg for lg in self.logs if lg.log_index < before]):
            if lg.log_index in self.used or len(lg.topics) != 3 or lg.topics[0] != TRANSFER:
                continue
            if (frm is None or _addr(lg.topics[1]) == frm) and (to is None or _addr(lg.topics[2]) == to):
                if _words(lg.data)[:1] == [amount]:
                    self.used.add(lg.log_index)
                    return lg.address
        return None

    def wrapped_logs(self, topic0: str, party_in_npm: bool = True) -> list[RawLog]:
        """包装原生币合约发出的、当事人是 NPM 的 Deposit / Withdrawal。"""
        wrapped = self.run.ctx.wrapped_native
        return [
            lg
            for lg in self.logs
            if lg.address == wrapped and lg.topics[:1] == [topic0] and (_addr(lg.topics[1]) in self.npm) == party_in_npm
        ]


@dataclass(frozen=True)
class UniswapV3Decoder:
    family: str
    instance_key: str
    decoder_version: str
    position_managers: frozenset[str]

    def decode(self, run: FamilyRun) -> None:
        npm_logs = [lg for lg in run.logs if lg.address in self.position_managers]
        if not npm_logs:
            return
        calls = parse_npm_calls(run.tx.input) if run.is_call_target else NpmCalls((), (), ())
        tx = _Tx(run, self.position_managers, calls, sorted(run.receipt.logs, key=lambda lg: lg.log_index))
        decreases: dict[int, tuple[int, int, int, RawLog]] = {}
        paid_native = False
        for log in npm_logs:
            t0 = log.topics[0]
            if t0 == TRANSFER and len(log.topics) == 4:
                self._nft_transfer(run, log)
            elif t0 == INCREASE:
                paid_native |= self._increase(tx, log)
            elif t0 == DECREASE:
                tid = int(log.topics[1], 16)
                liquidity, a0, a1 = _words(log.data)[:3]
                decreases[tid] = (a0, a1, liquidity, log)
            elif t0 == NPM_COLLECT:
                self._collect(tx, log, decreases.pop(int(log.topics[1], 16), None))
        for tid, (a0, a1, liquidity, log) in decreases.items():
            # 只减流动性不领取：本金留在 NPM 的 tokensOwed 里，没有资产流动
            run.state_event(
                EventType.INFORMATIONAL,
                EventSubtype.NONE,
                evidence=log,
                asset=None,
                amount_raw=None,
                position_key=self._key(run, tid),
                extra={"action": "decrease_liquidity", "liquidity": liquidity, "amount0": a0, "amount1": a1},
            )
        if paid_native:
            self._refund(tx)

    # ------------------------------------------------------------------

    def _key(self, run: FamilyRun, token_id: int) -> str:
        return PositionRef(run.ctx.chain, self.instance_key, PositionKind.NFT, str(token_id), run.subject).key

    def _nft_transfer(self, run: FamilyRun, log: RawLog) -> None:
        frm, to, tid = _addr(log.topics[1]), _addr(log.topics[2]), int(log.topics[3], 16)
        if run.subject not in (frm, to):
            return
        flows = run.ledger.find(
            lambda f: f.kind is AssetFlowKind.ERC721 and f.asset == log.address and f.token_id == tid
        )
        if not flows:
            return
        if frm == _ZERO:
            kind, direction = EventType.MINT, Direction.IN
        elif to == _ZERO:
            kind, direction = EventType.BURN, Direction.OUT
        else:
            kind, direction = EventType.TRANSFER, Direction.OUT if frm == run.subject else Direction.IN
        counterparty = None if kind is not EventType.TRANSFER else (to if direction is Direction.OUT else frm)
        run.claim_event(
            kind,
            EventSubtype.NONE,
            direction,
            [flows[0].flow_id],
            position_key=self._key(run, tid),
            counterparty=counterparty,
        )

    def _increase(self, tx: _Tx, log: RawLog) -> bool:
        """认领钱包付给池子的 token；返回这次是否用交易 value 付了原生币（之后要看退款）。

        先把两边的付款流水和 token 都找出来，再统一产出事件，保证两条事件的 extra 一致（都带 token0、token1）。
        """
        run, tid = tx.run, int(log.topics[1], 16)
        liquidity, a0, a1 = _words(log.data)[:3]
        mint = tx.pool_event(POOL_MINT, (a0, a1), log.log_index, data_offset=2)
        pool = mint.address if mint else None
        extra: dict = {"pool": pool, "liquidity": liquidity}
        if mint:
            extra |= {"tick_lower": _int24(mint.topics[2]), "tick_upper": _int24(mint.topics[3])}
        payments: list[int] = []  # 钱包这边的付款流水
        paid_native = False
        for side, amount in (("token0", a0), ("token1", a1)):
            if amount == 0:
                continue
            flows = run.ledger.find(
                lambda f, amount=amount: (
                    f.kind is AssetFlowKind.ERC20
                    and f.from_address == run.subject
                    and (pool is None or f.to_address == pool)
                    and f.amount_raw == amount
                    and f.flow_id not in payments
                )
            )
            if flows:
                extra[side] = flows[0].asset
                payments.append(flows[0].flow_id)
                continue
            token = tx.token_of(amount, frm=None, to=pool, before=mint.log_index if mint else log.log_index)
            extra[side] = token
            if token is not None and token == run.ctx.wrapped_native:
                # 付的是原生币：NPM 把交易 value 包装成 WETH 付给池子，钱包这边的流水是交易 value
                value = run.ledger.find(
                    lambda f: (
                        f.kind is AssetFlowKind.NATIVE and f.source is FlowSource.TX and f.from_address == run.subject
                    )
                )
                if value:
                    payments.append(value[0].flow_id)
                    paid_native = True
        key = self._key(run, tid)
        for fid in payments:
            run.claim_event(
                EventType.DEPOSIT,
                EventSubtype.DEPOSIT_ASSET,
                Direction.OUT,
                [fid],
                position_key=key,
                counterparty=pool,
                extra=extra,
            )
        return paid_native

    def _refund(self, tx: _Tx) -> None:
        """付原生币时 NPM 只包装实际用到的数量，多付的由 refundETH 退回：退款 = value − Σ Deposit(dst=NPM)。"""
        run = tx.run
        deposits = tx.wrapped_logs(DEPOSIT)
        refund = run.tx.value - sum(_words(lg.data)[0] for lg in deposits)
        if refund <= 0 or not tx.calls.refunds_eth:
            return
        npm = run.tx.to_address or next(iter(self.position_managers))
        evidence = deposits[-1] if deposits else next(lg for lg in run.logs if lg.topics[0] == INCREASE)
        fid = run.infer(AssetFlowKind.NATIVE, NATIVE, refund, npm, run.subject, evidence=evidence)
        if fid is not None:
            run.claim_event(
                EventType.WITHDRAWAL,
                EventSubtype.REMOVE_ASSET,
                Direction.IN,
                [fid],
                counterparty=npm,
                extra={"refund": True},
            )

    def _collect(self, tx: _Tx, log: RawLog, decrease: tuple[int, int, int, RawLog] | None) -> None:
        run, tid = tx.run, int(log.topics[1], 16)
        w = _words(log.data)
        recipient, amounts = _addr(w[0]), (w[1], w[2])
        pool_collect = tx.pool_event(POOL_COLLECT, amounts, log.log_index, data_offset=1)
        pool = pool_collect.address if pool_collect else None
        principal = (decrease[0], decrease[1]) if decrease else None
        key = self._key(run, tid)
        for i, amount in enumerate(amounts):
            if amount == 0:
                continue
            token = tx.token_of(
                amount, frm=pool, to=recipient, before=pool_collect.log_index if pool_collect else log.log_index
            )
            fid = self._collected_flow(tx, log, token, amount, pool, recipient)
            if fid is None:
                continue
            extra = {"pool": pool, "token_index": i}
            d = principal[i] if principal else None
            if d is None:
                run.claim_event(
                    EventType.CLAIM,
                    EventSubtype.LP_FEE,
                    Direction.IN,
                    [fid],
                    position_key=key,
                    counterparty=pool,
                    extra=extra | {"split_deferred": True},
                )
            elif amount < d:
                # 领取数量少于本次减掉的本金：collect 的 amountMax 限制了只领一部分，其余继续挂起。
                # 这一笔全是本金，挂起部分由 M3 结转
                run.claim_event(
                    EventType.WITHDRAWAL,
                    EventSubtype.REMOVE_ASSET,
                    Direction.IN,
                    [fid],
                    position_key=key,
                    counterparty=pool,
                    extra=extra | {"split_deferred": True},
                )
            elif d == 0:
                run.claim_event(
                    EventType.CLAIM,
                    EventSubtype.LP_FEE,
                    Direction.IN,
                    [fid],
                    position_key=key,
                    counterparty=pool,
                    extra=extra,
                )
            elif amount == d:
                run.claim_event(
                    EventType.WITHDRAWAL,
                    EventSubtype.REMOVE_ASSET,
                    Direction.IN,
                    [fid],
                    position_key=key,
                    counterparty=pool,
                    extra=extra,
                )
            else:
                principal_id, fee_id = run.split(fid, [d, amount - d])
                run.claim_event(
                    EventType.WITHDRAWAL,
                    EventSubtype.REMOVE_ASSET,
                    Direction.IN,
                    [principal_id],
                    position_key=key,
                    counterparty=pool,
                    extra=extra,
                )
                run.claim_event(
                    EventType.CLAIM,
                    EventSubtype.LP_FEE,
                    Direction.IN,
                    [fee_id],
                    position_key=key,
                    counterparty=pool,
                    extra=extra,
                )

    def _collected_flow(
        self, tx: _Tx, log: RawLog, token: str | None, amount: int, pool: str | None, recipient: str
    ) -> int | None:
        """找到（或推断）领取的这一边落到钱包上的那条流水。"""
        run = tx.run
        if recipient == run.subject:
            flows = run.ledger.find(
                lambda f: (
                    f.kind is AssetFlowKind.ERC20
                    and f.to_address == run.subject
                    and f.amount_raw == amount
                    and (pool is None or f.from_address == pool)
                    and (token is None or f.asset == token)
                )
            )
            return flows[0].flow_id if flows else None
        if recipient not in self.position_managers:
            return None  # 收款方是别人：这一边不属于钱包
        # 收款方是 NPM：随后由 unwrapWETH9 或 sweepToken 转出
        if token is not None and token == run.ctx.wrapped_native and run.subject in tx.calls.unwrap_recipients:
            evidence = next((lg for lg in tx.wrapped_logs(WITHDRAWAL) if lg.log_index > log.log_index), None)
            if evidence is None:
                run.warn(WarningCode.INTERNAL_UNAVAILABLE, f"日志 {log.log_index} 的 unwrapWETH9 找不到 Withdrawal")
                return None
            return run.infer(AssetFlowKind.NATIVE, NATIVE, amount, recipient, run.subject, evidence=evidence)
        if token is not None and (token, run.subject) in tx.calls.sweeps:
            flows = run.ledger.find(
                lambda f: (
                    f.kind is AssetFlowKind.ERC20
                    and f.asset == token
                    and f.from_address == recipient
                    and f.to_address == run.subject
                    and f.amount_raw == amount
                )
            )
            return flows[0].flow_id if flows else None
        run.warn(WarningCode.INTERNAL_UNAVAILABLE, f"日志 {log.log_index} 领取到 NPM 后的去向无法确定")
        return None
