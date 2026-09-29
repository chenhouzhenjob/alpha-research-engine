"""第二段分派：把交易交给相关协议实例的解码器（EVM，纯函数，规划 5.4 第 ② 步）。

分派顺序（设计文档 3.3）：
1. 按日志的发出地址：发出合约的识别结果属于哪个实例，这条日志就交给那个实例；
2. 按交易的 `to`：调用的合约属于某个实例时，该实例也参与，即使它没有发出日志
   （路由、聚合器只能从调用方法判断语义）；
3. 按 topic0 的通用规则：目前没有，保留位置。

每个实例在一笔交易里只调用一次，一次拿到分派给它的全部日志，以便处理整笔交易的上下文
（V3 的 multicall、Venus 的清算）。调用顺序按"交易 `to` 所属实例最先，其余按第一条日志的序号"，
保证推断流水的 flow_id 在同一输入下稳定。

解码器只通过 `FamilyRun` 读交易、推断流水、认领流水、产出事件，不直接改流水账。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from alpha_core.chain_data import RawLog, TxInfo, TxReceipt

from ..claims import FlowLedger
from ..context import DecodeContext
from ..events import EventDraft, position_of
from ..models import (
    AssetFlowKind,
    Confidence,
    CoverageTier,
    DecodeWarning,
    Direction,
    EventSubtype,
    EventType,
    FlowSource,
    NormalizedEvent,
    WarningCode,
)


class FamilyDecoder(Protocol):
    """协议实例的解码器。由家族按实例部署构造（`ProtocolFamily.decoder`），分派时调用。"""

    family: str  # 家族键
    instance_key: str  # 实例键
    decoder_version: str  # `<family>@<版本>`

    def decode(self, run: FamilyRun) -> None:
        """处理一笔交易：按需推断流水、认领流水、产出事件。没有相关内容时什么都不做。"""


@dataclass
class FamilyRun:
    """一个实例在一笔交易里的一次解码调用：只读的交易视图 + 推断、认领、产出事件的操作。"""

    tx: TxInfo
    receipt: TxReceipt
    subject: str  # 主体钱包，小写
    ctx: DecodeContext
    logs: tuple[RawLog, ...]  # 分派给本实例的日志（按 log_index 升序）
    decoder: FamilyDecoder
    ledger: FlowLedger
    drafts: list[EventDraft] = field(default_factory=list)

    @property
    def is_call_target(self) -> bool:
        """交易是否直接调用了本实例的合约。"""
        identity = self.ctx.identities.get(self.tx.to_address or "")
        return identity is not None and identity.instance_key == self.decoder.instance_key

    def infer(
        self,
        kind: AssetFlowKind,
        asset: str,
        amount_raw: int,
        from_address: str,
        to_address: str,
        *,
        evidence: RawLog,
        token_id: int | None = None,
    ) -> int | None:
        """补一条推断流水（见 `FlowLedger.infer`）。"""
        return self.ledger.infer(
            kind, asset, amount_raw, from_address, to_address, evidence_log_index=evidence.log_index, token_id=token_id
        )

    def split(self, flow_id: int, amounts: Sequence[int]) -> list[int]:
        """把一条流水拆成几条子流水（见 `FlowLedger.split`），返回子流水的 flow_id。"""
        return self.ledger.split(flow_id, amounts, self.decoder.decoder_version)

    def warn(self, code: WarningCode, detail: str) -> None:
        self.ledger.warnings.append(DecodeWarning(code, f"{self.decoder.instance_key}：{detail}"))

    def claim_event(
        self,
        event_type: EventType,
        event_subtype: EventSubtype,
        direction: Direction,
        flow_ids: Sequence[int],
        *,
        tier: CoverageTier = CoverageTier.T2,
        position_key: str | None = None,
        counterparty: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """认领流水并产出一条流动事件。被认领的流水必须是同一种资产；数量为它们的合计。

        @raises DecodeConflictError 流水已被认领
        @raises ValueError 没有流水，或流水不是同一种资产
        """
        if not flow_ids:
            raise ValueError("流动事件必须认领至少一条流水；没有资产流动的事件用 state_event")
        flows = [self.ledger.get(i) for i in flow_ids]
        if len({(f.asset, f.token_id) for f in flows}) != 1:
            raise ValueError(f"一条事件只能认领同一种资产的流水：{[(f.asset, f.token_id) for f in flows]}")
        self.ledger.claim(flow_ids, self.decoder.decoder_version)
        inferred = any(f.source is FlowSource.INFERRED for f in flows)
        event = self._event(
            event_type,
            event_subtype,
            direction,
            tier=tier,
            asset=flows[0].asset,
            token_id=flows[0].token_id,
            amount_raw=sum(f.amount_raw for f in flows),
            claimed=tuple(flow_ids),
            position_key=position_key,
            counterparty=counterparty,
            confidence=Confidence.INFERRED if inferred else Confidence.EXACT,
            extra=extra,
        )
        self.drafts.append(EventDraft(position_of(event, {f.flow_id: f for f in flows}), event))

    def state_event(
        self,
        event_type: EventType,
        event_subtype: EventSubtype,
        *,
        evidence: RawLog,
        asset: str | None,
        amount_raw: int | None,
        tier: CoverageTier = CoverageTier.T2,
        position_key: str | None = None,
        counterparty: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """产出一条没有资产流动的状态事件（例如被清算时负债减少），位置在依据的日志处。"""
        event = self._event(
            event_type,
            event_subtype,
            Direction.NEUTRAL,
            tier=tier,
            asset=asset,
            token_id=None,
            amount_raw=amount_raw,
            claimed=(),
            position_key=position_key,
            counterparty=counterparty,
            confidence=Confidence.EXACT,
            extra=extra,
        )
        self.drafts.append(EventDraft(position_of(event, {}, evidence.log_index), event))

    def _event(
        self, event_type: EventType, event_subtype: EventSubtype, direction: Direction, **fields: Any
    ) -> NormalizedEvent:
        extra = fields.pop("extra")
        return NormalizedEvent(
            chain=self.ctx.chain,
            tx_hash=self.tx.tx_hash,
            seq=0,
            subject_wallet=self.subject,
            event_type=event_type,
            event_subtype=event_subtype,
            direction=direction,
            decoder_version=self.decoder.decoder_version,
            coverage_tier=fields.pop("tier"),
            claimed_flow_ids=fields.pop("claimed"),
            counterparty_address=fields.pop("counterparty"),
            family=self.decoder.family,
            instance_key=self.decoder.instance_key,
            extra=dict(extra or {}),
            **fields,
        )


def _groups(
    tx: TxInfo, receipt: TxReceipt, ctx: DecodeContext, decoders: Mapping[str, FamilyDecoder]
) -> list[tuple[str, tuple[RawLog, ...]]]:
    """按实例分组日志，并排好调用顺序。"""
    logs_by_instance: dict[str, list[RawLog]] = {}
    for log in sorted(receipt.logs, key=lambda lg: lg.log_index):
        identity = ctx.identities.get(log.address)
        if identity is not None and identity.instance_key in decoders:
            logs_by_instance.setdefault(identity.instance_key, []).append(log)
    target = ctx.identities.get(tx.to_address or "")
    target_key = target.instance_key if target is not None and target.instance_key in decoders else None
    if target_key is not None:
        logs_by_instance.setdefault(target_key, [])

    def order(key: str) -> tuple[int, int]:
        if key == target_key:
            return (0, -1)
        return (1, logs_by_instance[key][0].log_index)

    return [(k, tuple(logs_by_instance[k])) for k in sorted(logs_by_instance, key=order)]


def run_families(
    tx: TxInfo,
    receipt: TxReceipt,
    subject: str,
    ctx: DecodeContext,
    decoders: Mapping[str, FamilyDecoder],
    ledger: FlowLedger,
) -> list[EventDraft]:
    """按分派顺序调用各实例的解码器，返回它们产出的事件。失败的交易不分派（只有 gas）。

    @raises DecodeConflictError 两个解码器认领了同一条流水
    """
    if receipt.status == 0:
        return []
    drafts: list[EventDraft] = []
    for instance_key, logs in _groups(tx, receipt, ctx, decoders):
        run = FamilyRun(tx, receipt, subject, ctx, logs, decoders[instance_key], ledger)
        run.decoder.decode(run)
        drafts += run.drafts
    return drafts


def instance_keys(decoders: Iterable[FamilyDecoder]) -> dict[str, FamilyDecoder]:
    """把解码器列表转成按实例键索引的映射。"""
    return {d.instance_key: d for d in decoders}
