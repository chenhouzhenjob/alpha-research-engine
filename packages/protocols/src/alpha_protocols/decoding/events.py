"""事件的排序与定稿：给事件分配稳定的交易内序号 `seq`，并按分类表校验。

M3 的人工修正按 `(chain, tx_hash, seq, subject_wallet)` 定位事件，所以同一输入重复解码，
`seq` 必须完全相同。排序键由事件在交易里的位置决定（见 `position_of`），位置相同时再按事件
内容排序，不依赖家族产出事件的先后顺序。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from .models import AssetFlow, AssetFlowKind, FlowSource, NormalizedEvent
from .taxonomy import validate_event

# 事件在交易里的位置分段：交易级流水（系统铸币、value）在最前，日志按 log_index，
# 内部交易其次，gas 在最后。
STAGE_TX_HEAD, STAGE_LOG, STAGE_INTERNAL, STAGE_GAS = 0, 1, 2, 3


@dataclass(frozen=True)
class EventDraft:
    """尚未分配 seq 的事件。`position` 是排序键：(分段, 日志序号或流水序号)。"""

    position: tuple[int, int]
    event: NormalizedEvent


def position_of_flow(flow: AssetFlow) -> tuple[int, int]:
    """一条流水在交易里的位置。推断出的流水带着证据日志的序号，排在证据日志的位置。"""
    if flow.kind is AssetFlowKind.GAS:
        return (STAGE_GAS, flow.flow_id)
    if flow.log_index is not None:
        return (STAGE_LOG, flow.log_index)
    if flow.source is FlowSource.INTERNAL:
        return (STAGE_INTERNAL, flow.flow_id)
    return (STAGE_TX_HEAD, flow.flow_id)


def position_of(
    event: NormalizedEvent, flows: Mapping[int, AssetFlow], log_index: int | None = None
) -> tuple[int, int]:
    """事件的位置：流动事件取它认领的第一条流水；状态事件和信息事件取它依据的日志。

    @raises ValueError 状态事件没有给出依据的日志序号
    """
    if event.claimed_flow_ids:
        return min(position_of_flow(flows[i]) for i in event.claimed_flow_ids)
    if log_index is None:
        raise ValueError(
            f"事件 {event.event_type.value}/{event.event_subtype.value} 没有认领流水，必须给出依据的日志序号"
        )
    return (STAGE_LOG, log_index)


def finalize(drafts: Iterable[EventDraft]) -> tuple[NormalizedEvent, ...]:
    """按位置排序、分配 seq、校验分类表。

    @raises TaxonomyError 有事件不符合分类表
    """

    def sort_key(d: EventDraft) -> tuple:
        e = d.event
        return (
            d.position,
            e.event_type.value,
            e.event_subtype.value,
            e.direction.value,
            e.asset or "",
            e.claimed_flow_ids,
        )

    out = []
    for seq, draft in enumerate(sorted(drafts, key=sort_key)):
        event = replace(draft.event, seq=seq)
        validate_event(event)
        out.append(event)
    return tuple(out)
