"""Compound V2 系（Venus 等）的钱包视角解码：存款、取款、借款、还款、清算、领奖励（纯函数，规划 5.8）。

市场（vToken）发出的日志和 Comptroller 的日志分派到这里。市场由识别结果给出（kind = market，
来自 Comptroller.getAllMarkets() 的注册表发现）。

**以资产流水判断钱包视角**：事件里的当事人（minter、redeemer、borrower、payer）可能是中间合约而不是用户，
所以只在"钱包自己是当事人"时产出存取、借款事件；别人替钱包存入（vToken 留在中间合约）不算钱包的存款。

**事件变体**：Venus 新版市场的 Mint / Redeem 多一个 accountBalance 字段，vBNB 等老市场是 Compound 原版
3 个字段，同一个实例里两种并存（2026-09-29 实测）。两种签名都登记，按 topic0 识别，字段前缀相同。

事件映射（"当事人"指事件里的 minter / redeemer / borrower / payer / liquidator）：
- `Mint`：当事人是钱包时，付出的底层记 `deposit/deposit_asset`，收到的 vToken 记 `receive/receive_wrapped`；
- `Redeem`：当事人是钱包时，交回的 vToken 记 `spend/return_wrapped`，收到的底层记 `withdrawal/remove_asset`；
- `Borrow`：借款人是钱包时记 `borrow/generate_debt`；
- `RepayBorrow`：还款人是钱包时记 `repay/payback_debt`（out，替别人还时 extra.on_behalf）；
  借款人是钱包、还款人是别人时，钱包没有资产流动，记状态事件 `repay/payback_debt`（neutral）；
- `LiquidateBorrow`：借款人视角记 `repay/liquidate`（状态事件，数量为负债减少额），被转走的抵押 vToken 记
  `spend/liquidate`；清算人视角付出的还款资产、得到的抵押 vToken 各记一条 `trade/liquidate`；
- Comptroller 转给钱包的奖励 token 记 `claim/reward`。

原生币市场（vBNB）的原生币推断：Redeem 的 redeemAmount、Borrow 的 borrowAmount 就是市场转给当事人的
原生币数量（规划 5.5，已用样本逐 wei 核对）。清算里的 RepayBorrow 由 LiquidateBorrow 统一处理，不重复计。
"""

from __future__ import annotations

from dataclasses import dataclass

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

MINT = frozenset({event_topic("Mint(address,uint256,uint256)"), event_topic("Mint(address,uint256,uint256,uint256)")})
REDEEM = frozenset(
    {event_topic("Redeem(address,uint256,uint256)"), event_topic("Redeem(address,uint256,uint256,uint256)")}
)
BORROW = event_topic("Borrow(address,uint256,uint256,uint256)")
REPAY = event_topic("RepayBorrow(address,address,uint256,uint256,uint256)")
LIQUIDATE = event_topic("LiquidateBorrow(address,address,uint256,address,uint256)")
MARKET_KIND = "market"  # 识别结果里市场的 kind


def _words(data: str) -> list[int]:
    raw = data.removeprefix("0x")
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def _addr(word: int) -> str:
    return "0x" + format(word, "064x")[-40:]


@dataclass(frozen=True)
class CompoundV2Decoder:
    family: str
    instance_key: str
    decoder_version: str
    comptrollers: frozenset[str]
    native_market: str | None  # 底层资产为原生币的市场（vBNB）
    reward_token: str | None  # 协议奖励 token（XVS）

    def decode(self, run: FamilyRun) -> None:
        markets = {
            a for a, i in run.ctx.identities.items() if i.instance_key == self.instance_key and i.kind == MARKET_KIND
        }
        logs = [lg for lg in run.logs if lg.address in markets and lg.topics]
        liquidations = [lg for lg in logs if lg.topics[0] == LIQUIDATE]
        liquidated = {(lg.address, _addr(_words(lg.data)[1]), _words(lg.data)[2]) for lg in liquidations}
        for log in logs:
            t0, w = log.topics[0], _words(log.data)
            if t0 in MINT and _addr(w[0]) == run.subject:
                self._mint(run, log, w[1], w[2])
            elif t0 in REDEEM and _addr(w[0]) == run.subject:
                self._redeem(run, log, w[1], w[2])
            elif t0 == BORROW and _addr(w[0]) == run.subject:
                self._borrow(run, log, w[1], w[2])
            elif t0 == REPAY and (log.address, _addr(w[1]), w[2]) not in liquidated:
                self._repay(run, log, _addr(w[0]), _addr(w[1]), w[2], w[3])
            elif t0 == LIQUIDATE:
                self._liquidate(run, log, _addr(w[0]), _addr(w[1]), w[2], _addr(w[3]), w[4])
        if self.reward_token and (run.is_call_target or any(lg.address in self.comptrollers for lg in run.logs)):
            self._rewards(run)

    # ------------------------------------------------------------------

    def _key(self, run: FamilyRun, kind: PositionKind, market: str) -> str:
        return PositionRef(run.ctx.chain, self.instance_key, kind, market, run.subject).key

    def _find(self, run: FamilyRun, kind: AssetFlowKind, amount: int, frm: str, to: str, asset: str | None = None):
        flows = run.ledger.find(
            lambda f: (
                f.kind is kind
                and f.amount_raw == amount
                and f.from_address == frm
                and f.to_address == to
                and (asset is None or f.asset == asset)
            )
        )
        return flows[0].flow_id if flows else None

    def _underlying_out(self, run: FamilyRun, log: RawLog, market: str, amount: int) -> int | None:
        """钱包付给市场的底层资产流水：ERC20 转账，或原生币市场的交易 value。"""
        if market == self.native_market:
            fid = self._find(run, AssetFlowKind.NATIVE, amount, run.subject, market)
            if fid is None:
                run.warn(WarningCode.INTERNAL_UNAVAILABLE, f"日志 {log.log_index} 付给 {market} 的原生币不是交易 value")
            return fid
        return self._find(run, AssetFlowKind.ERC20, amount, run.subject, market)

    def _underlying_in(self, run: FamilyRun, log: RawLog, market: str, amount: int) -> int | None:
        """市场转给钱包的底层资产：ERC20 转账，或原生币市场推断出的原生币。"""
        if market == self.native_market:
            return run.infer(AssetFlowKind.NATIVE, NATIVE, amount, market, run.subject, evidence=log)
        return self._find(run, AssetFlowKind.ERC20, amount, market, run.subject)

    def _underlying_asset(self, run: FamilyRun, market: str, amount: int) -> str | None:
        """状态事件要写明资产：原生币市场为 NATIVE，其他市场从同一笔交易里转给市场的 ERC20 转账找。"""
        if market == self.native_market:
            return NATIVE
        for lg in run.receipt.logs:
            if lg.topics[:1] == [TRANSFER] and len(lg.topics) == 3 and "0x" + lg.topics[2][-40:] == market:
                if _words(lg.data)[:1] == [amount]:
                    return lg.address
        return None

    def _mint(self, run: FamilyRun, log: RawLog, amount: int, vtokens: int) -> None:
        market, key = log.address, self._key(run, PositionKind.SHARE, log.address)
        paid = self._underlying_out(run, log, market, amount)
        got = self._find(run, AssetFlowKind.ERC20, vtokens, market, run.subject, asset=market)
        if paid is not None:
            run.claim_event(
                EventType.DEPOSIT,
                EventSubtype.DEPOSIT_ASSET,
                Direction.OUT,
                [paid],
                position_key=key,
                counterparty=market,
            )
        if got is not None:
            run.claim_event(
                EventType.RECEIVE,
                EventSubtype.RECEIVE_WRAPPED,
                Direction.IN,
                [got],
                position_key=key,
                counterparty=market,
            )

    def _redeem(self, run: FamilyRun, log: RawLog, amount: int, vtokens: int) -> None:
        market, key = log.address, self._key(run, PositionKind.SHARE, log.address)
        returned = self._find(run, AssetFlowKind.ERC20, vtokens, run.subject, market, asset=market)
        got = self._underlying_in(run, log, market, amount)
        if returned is not None:
            run.claim_event(
                EventType.SPEND,
                EventSubtype.RETURN_WRAPPED,
                Direction.OUT,
                [returned],
                position_key=key,
                counterparty=market,
            )
        if got is not None:
            run.claim_event(
                EventType.WITHDRAWAL,
                EventSubtype.REMOVE_ASSET,
                Direction.IN,
                [got],
                position_key=key,
                counterparty=market,
            )

    def _borrow(self, run: FamilyRun, log: RawLog, amount: int, account_borrows: int) -> None:
        got = self._underlying_in(run, log, log.address, amount)
        if got is not None:
            run.claim_event(
                EventType.BORROW,
                EventSubtype.GENERATE_DEBT,
                Direction.IN,
                [got],
                position_key=self._key(run, PositionKind.DEBT, log.address),
                counterparty=log.address,
                extra={"account_borrows": account_borrows},
            )

    def _repay(self, run: FamilyRun, log: RawLog, payer: str, borrower: str, amount: int, account_borrows: int) -> None:
        market = log.address
        extra = {"borrower": borrower, "payer": payer, "account_borrows": account_borrows}
        if payer == run.subject:
            paid = self._underlying_out(run, log, market, amount)
            if paid is not None:
                own = borrower == run.subject
                run.claim_event(
                    EventType.REPAY,
                    EventSubtype.PAYBACK_DEBT,
                    Direction.OUT,
                    [paid],
                    position_key=self._key(run, PositionKind.DEBT, market) if own else None,
                    counterparty=market,
                    extra=extra | {"on_behalf": not own},
                )
        elif borrower == run.subject:
            # 别人替钱包还款：钱包没有资产流动，只是负债减少
            run.state_event(
                EventType.REPAY,
                EventSubtype.PAYBACK_DEBT,
                evidence=log,
                asset=self._underlying_asset(run, market, amount),
                amount_raw=amount,
                position_key=self._key(run, PositionKind.DEBT, market),
                counterparty=payer,
                extra=extra,
            )

    def _liquidate(
        self, run: FamilyRun, log: RawLog, liquidator: str, borrower: str, repay: int, collateral: str, seize: int
    ) -> None:
        market = log.address
        extra = {"liquidator": liquidator, "borrower": borrower, "collateral_market": collateral, "seize_tokens": seize}
        if borrower == run.subject:
            run.state_event(
                EventType.REPAY,
                EventSubtype.LIQUIDATE,
                evidence=log,
                asset=self._underlying_asset(run, market, repay),
                amount_raw=repay,
                position_key=self._key(run, PositionKind.DEBT, market),
                counterparty=liquidator,
                extra=extra,
            )
            # 被扣走的抵押 vToken：借款人转出的该 vToken 流水（Venus 会把一部分分给协议储备，都算被扣走）
            seized = run.ledger.find(
                lambda f: (
                    f.asset == collateral
                    and f.from_address == run.subject
                    and f.log_index is not None
                    and f.log_index < log.log_index
                )
            )
            for f in seized:
                run.claim_event(
                    EventType.SPEND,
                    EventSubtype.LIQUIDATE,
                    Direction.OUT,
                    [f.flow_id],
                    position_key=self._key(run, PositionKind.SHARE, collateral),
                    counterparty=f.to_address,
                    extra=extra,
                )
        if liquidator == run.subject:
            paid = (
                self._underlying_out(run, log, market, repay)
                if market != self.native_market
                else self._find(run, AssetFlowKind.NATIVE, repay, run.subject, market)
            )
            if paid is None:
                run.warn(
                    WarningCode.INTERNAL_UNAVAILABLE, f"日志 {log.log_index} 清算的还款资产看不到（可能来自内部调用）"
                )
            else:
                run.claim_event(
                    EventType.TRADE, EventSubtype.LIQUIDATE, Direction.OUT, [paid], counterparty=market, extra=extra
                )
            got = run.ledger.find(
                lambda f: (
                    f.asset == collateral
                    and f.to_address == run.subject
                    and f.log_index is not None
                    and f.log_index < log.log_index
                )
            )
            for f in got:
                run.claim_event(
                    EventType.TRADE,
                    EventSubtype.LIQUIDATE,
                    Direction.IN,
                    [f.flow_id],
                    counterparty=borrower,
                    extra=extra,
                )

    def _rewards(self, run: FamilyRun) -> None:
        flows = run.ledger.find(
            lambda f: (
                f.asset == self.reward_token
                and f.from_address in self.comptrollers
                and f.to_address == run.subject
                and f.source is FlowSource.LOG
            )
        )
        for f in flows:
            run.claim_event(
                EventType.CLAIM, EventSubtype.REWARD, Direction.IN, [f.flow_id], counterparty=f.from_address
            )
