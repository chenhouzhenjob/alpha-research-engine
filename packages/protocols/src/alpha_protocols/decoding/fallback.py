"""兜底：把没有被任何家族认领的资产流水转成事件（与链无关的纯函数，规划 5.4 第 ④ 步）。

兜底保证"任何交易都有正确的资产流动（T0）"：家族认不出的协议，钱包的余额变化也一条不漏。
规则只依据流水本身和 token 风险标记，不猜协议语义：

| 流水 | 事件 |
|---|---|
| gas | `fee/none`（out） |
| 系统交易铸造的原生币（L2 的 L1 存款） | `bridge/remove_asset`（in） |
| 风险 token（仿冒、垃圾空投）的转入或转出 | `receive/spam` |
| 别人发起的交易里、从钱包转出 0 数量的 token（地址投毒的典型手法） | `receive/spam`（out） |
| 别人发起的交易里、从铸币地址转给钱包的 token | `receive/airdrop`（in） |
| 其他 | `transfer/none`（in 或 out） |

钱包转给自己的流水（from 和 to 都是钱包）产出一进一出两条事件，数量相抵。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .context import DecodeContext
from .events import STAGE_LOG, EventDraft, position_of_flow
from .models import (
    AssetFlow,
    AssetFlowKind,
    Confidence,
    CoverageTier,
    Direction,
    EventSubtype,
    EventType,
    FlowSource,
    NormalizedEvent,
    RiskFlag,
)

DECODER_VERSION = "generic@1"
_TOKEN_KINDS = (AssetFlowKind.ERC20, AssetFlowKind.ERC721, AssetFlowKind.ERC1155)


def _classify(
    flow: AssetFlow, direction: Direction, tx_sender: str, subject: str, mint_source: str, ctx: DecodeContext
) -> tuple[EventType, EventSubtype, dict[str, Any]]:
    if flow.kind is AssetFlowKind.GAS:
        return EventType.FEE, EventSubtype.NONE, {}
    if flow.source is FlowSource.SYSTEM:
        return EventType.BRIDGE, EventSubtype.REMOVE_ASSET, {"reason": "system_mint"}
    if flow.kind in _TOKEN_KINDS:
        risk = ctx.risk_of(flow.asset)
        if risk in (RiskFlag.SPAM, RiskFlag.IMPERSONATOR):
            return EventType.RECEIVE, EventSubtype.SPAM, {"risk_flag": risk.value}
        not_initiated = tx_sender != subject
        if direction is Direction.OUT and flow.amount_raw == 0 and not_initiated:
            return EventType.RECEIVE, EventSubtype.SPAM, {"reason": "zero_transfer_from"}
        if direction is Direction.IN and flow.from_address == mint_source and not_initiated:
            return EventType.RECEIVE, EventSubtype.AIRDROP, {}
        extra = {"risk_flag": risk.value} if risk is not RiskFlag.NORMAL else {}
        return EventType.TRANSFER, EventSubtype.NONE, extra
    return EventType.TRANSFER, EventSubtype.NONE, {}


def fallback_events(
    flows: Iterable[AssetFlow],
    *,
    tx_hash: str,
    tx_sender: str,
    subject: str,
    ctx: DecodeContext,
    mint_source: str,
) -> list[EventDraft]:
    """为未认领的流水生成兜底事件。

    @param flows 未被家族认领的流水
    @param tx_sender 交易发起人；用来区分"钱包自己发起的"和"别人发起、落到钱包上的"
    @param mint_source token 铸币的来源地址（EVM 为零地址），由链相关的调用方传入
    """
    drafts: list[EventDraft] = []
    for flow in flows:
        directions = [
            d
            for d, hit in ((Direction.OUT, flow.from_address == subject), (Direction.IN, flow.to_address == subject))
            if hit
        ]
        for direction in directions:
            event_type, subtype, extra = _classify(flow, direction, tx_sender, subject, mint_source, ctx)
            counterparty = flow.to_address if direction is Direction.OUT else flow.from_address
            event = NormalizedEvent(
                chain=ctx.chain,
                tx_hash=tx_hash,
                seq=0,
                subject_wallet=subject,
                event_type=event_type,
                event_subtype=subtype,
                direction=direction,
                decoder_version=DECODER_VERSION,
                coverage_tier=CoverageTier.T0,
                asset=flow.asset,
                amount_raw=flow.amount_raw,
                token_id=flow.token_id,
                claimed_flow_ids=(flow.flow_id,),
                counterparty_address=None if flow.kind is AssetFlowKind.GAS else counterparty,
                confidence=Confidence.INFERRED if flow.source is FlowSource.INFERRED else Confidence.EXACT,
                extra=extra,
            )
            drafts.append(EventDraft(position_of_flow(flow), event))
    return drafts


def informational_event(
    *,
    ctx: DecodeContext,
    tx_hash: str,
    subject: str,
    subtype: EventSubtype,
    log_index: int,
    asset: str | None,
    extra: Mapping[str, Any],
    counterparty: str | None = None,
) -> EventDraft:
    """不动资产的信息事件（授权、未知合约的可读事件），位置在依据的日志处。"""
    event = NormalizedEvent(
        chain=ctx.chain,
        tx_hash=tx_hash,
        seq=0,
        subject_wallet=subject,
        event_type=EventType.INFORMATIONAL,
        event_subtype=subtype,
        direction=Direction.NEUTRAL,
        decoder_version=DECODER_VERSION,
        coverage_tier=CoverageTier.T0,
        asset=asset,
        counterparty_address=counterparty,
        extra={"log_index": log_index, **extra},
    )
    return EventDraft((STAGE_LOG, log_index), event)
