"""EVM 交易的解码入口（纯函数）。

回执路径 `decode_evm_tx` 的流程（规划 5.4）；索引路径 `decode_evm_transfers` 只有第 1 步（改读索引转账）、
第 3 步和第 5 步：
1. `flows.extract`：提取主体钱包参与的资产流水和授权，入流水账；
2. `dispatch.run_families`：各协议实例的解码器推断流水、认领流水、产出带协议语义的事件；
3. 兜底：未认领的流水转成 transfer / receive / fee / bridge 事件；授权转成 informational/approve；
4. 通用 ABI 解码：主体钱包交互范围内、未识别合约发出的日志，解成 informational/decoded_log；
5. `events.finalize`：分配稳定的 seq，按分类表校验。

"交互范围"指：日志由交易的 `to` 发出，或日志的 topic 里带着主体钱包。聚合器路由内部在池子之间
搬运产生的大量日志不在范围内，避免一笔交易产出几百条无意义的事件。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from alpha_core.chain_data import RawLog, TxInfo, TxReceipt
from alpha_core.ports import AddressTransfer

from ..claims import FlowLedger
from ..context import DecodeContext
from ..events import EventDraft, finalize
from ..fallback import fallback_events, informational_event
from ..models import (
    CoverageTier,
    DecodedTx,
    DecodeWarning,
    EventSubtype,
    InternalTransfer,
    NormalizedEvent,
    WarningCode,
)
from .abi_logs import decode_with_contract_abi, decode_with_signature
from .dispatch import FamilyDecoder, run_families
from .flows import TOKEN_STANDARD_TOPICS, Approval, extract, extract_from_transfers
from .rules import ChainRules

ZERO_ADDRESS = "0x" + "0" * 40  # EVM 上 token 铸币的来源地址


def _in_scope(log: RawLog, tx: TxInfo, subject: str) -> bool:
    """日志是否属于钱包的交互范围：钱包发起的交易里由交易目标发出，或 topic 里带钱包地址。

    别人发起的交易（批量打款、空投）里，交易目标的日志大多是关于其他收款人的，只算提到钱包的那些。
    """
    wallet_topic = "0x" + "0" * 24 + subject[2:]
    own_call = tx.from_address == subject and log.address == (tx.to_address or "")
    return own_call or wallet_topic in log.topics[1:]


def _decode_unknown_logs(
    receipt: TxReceipt, tx: TxInfo, subject: str, ctx: DecodeContext
) -> tuple[list[EventDraft], list[str], list[DecodeWarning]]:
    drafts: list[EventDraft] = []
    unknown: dict[str, None] = {}
    missing: dict[str, None] = {}
    for log in sorted(receipt.logs, key=lambda lg: lg.log_index):
        if not log.topics or log.topics[0] in TOKEN_STANDARD_TOPICS or not _in_scope(log, tx, subject):
            continue
        identity = ctx.identities.get(log.address)
        # 只有识别出家族的合约才算"认识"；识别结果是 unknown / eoa 的仍按未知合约处理（报出来、尝试 ABI 解码）
        if (identity is not None and identity.family is not None) or log.address in ctx.tokens:
            continue
        unknown[log.address] = None
        decoded = None
        if log.address in ctx.contract_abis:
            decoded = decode_with_contract_abi(log, ctx.contract_abis[log.address])
        for signature in ctx.event_signatures.get(log.topics[0], ()):
            if decoded is not None:
                break
            decoded = decode_with_signature(log, signature)
        if decoded is None:
            missing[log.address] = None
            continue
        drafts.append(
            informational_event(
                ctx=ctx,
                tx_hash=tx.tx_hash,
                subject=subject,
                subtype=EventSubtype.DECODED_LOG,
                log_index=log.log_index,
                asset=None,
                counterparty=log.address,
                extra={
                    "event": decoded.event,
                    "signature": decoded.signature,
                    "args": dict(decoded.args),
                    "abi_source": decoded.source.value,
                },
            )
        )
    warnings = [DecodeWarning(WarningCode.ABI_MISSING, f"合约 {a} 的日志没有能对上的 ABI") for a in missing]
    return drafts, list(unknown), warnings


def decode_evm_tx(
    tx: TxInfo,
    receipt: TxReceipt,
    subject: str,
    ctx: DecodeContext,
    rules: ChainRules,
    *,
    decoders: Mapping[str, FamilyDecoder] | None = None,
    internal: Sequence[InternalTransfer] | None = None,
) -> DecodedTx:
    """解码一笔 EVM 交易在主体钱包视角下的事件。

    @param decoders 实例键 → 解码器；不传只做通用解码（T0）。通常由 `alpha_protocols.runtime` 组装
    @param internal 数据源给出的内部交易；None 表示数据源不可用
    @raises ValueError 计算 gas 缺少必要字段
    @raises DecodeConflictError 两个解码器认领了同一条流水
    @raises TaxonomyError 产出的事件不符合分类表（说明规则有 bug）
    """
    subject = subject.lower()
    extracted = extract(tx, receipt, subject, rules, internal)
    ledger = FlowLedger(extracted.flows, internal_available=internal is not None)
    drafts: list[EventDraft] = run_families(tx, receipt, subject, ctx, decoders or {}, ledger)
    drafts += fallback_events(
        ledger.unclaimed(),
        tx_hash=tx.tx_hash,
        tx_sender=tx.from_address,
        subject=subject,
        ctx=ctx,
        mint_source=ZERO_ADDRESS,
    )
    drafts += _approval_events(extracted.approvals, tx, subject, ctx)
    decoded, unknown, warnings = (
        _decode_unknown_logs(receipt, tx, subject, ctx) if receipt.status != 0 else ([], [], [])
    )
    drafts += decoded
    warnings += [
        DecodeWarning(WarningCode.MALFORMED_LOG, f"日志 {i} 格式不符合标准") for i in extracted.malformed_log_indexes
    ]
    warnings += ledger.warnings
    events = tuple(_upgrade_valuable(e, ctx) for e in finalize(drafts))
    # 已认领 = 事件认领的 + 流水账里被家族认领的（例如被拆分的父流水，由子流水的事件认领）
    claimed = {i for e in events for i in e.claimed_flow_ids} | {
        f.flow_id for f in ledger.flows if ledger.is_claimed(f.flow_id)
    }
    return DecodedTx(
        chain=ctx.chain,
        tx_hash=tx.tx_hash,
        subject_wallet=subject,
        succeeded=receipt.status != 0,
        events=events,
        flows=ledger.flows,
        unclaimed_flow_ids=tuple(f.flow_id for f in ledger.flows if f.flow_id not in claimed),
        unknown_contracts=tuple(unknown),
        warnings=tuple(warnings),
    )


def decode_evm_transfers(
    tx: TxInfo,
    receipt: TxReceipt,
    transfers: Sequence[AddressTransfer],
    subject: str,
    ctx: DecodeContext,
    rules: ChainRules,
    *,
    internal: Sequence[InternalTransfer] | None = None,
) -> DecodedTx:
    """索引路径：只凭地址索引源的转账解码一笔交易（M3 规划 5.4），产出 T0 事件。

    用于不取回执的交易（第三方发起、只涉及转账，以及标准 gas 模型下钱包发起的普通原生币转账）。
    与回执路径共用流水账、兜底和风险标记，所以资产事件和回执路径不走家族解码时逐条相同；
    没有日志可读，因此不产出授权事件和通用 ABI 解码的事件，也不跑家族解码。

    @param receipt 由索引字段构造的回执（status、gas_used、effective_gas_price；logs 为空）
    @param transfers 这笔交易里和钱包有关的代币转账（原生币条目忽略，见 `extract_from_transfers`）
    @param internal 内部交易；None 表示数据源不可用
    @raises ValueError 计算 gas 缺少必要字段，或代币转账缺日志序号
    @raises TaxonomyError 产出的事件不符合分类表（说明规则有 bug）
    """
    subject = subject.lower()
    extracted = extract_from_transfers(tx, receipt, subject, rules, transfers, internal)
    ledger = FlowLedger(extracted.flows, internal_available=internal is not None)
    drafts = fallback_events(
        ledger.unclaimed(),
        tx_hash=tx.tx_hash,
        tx_sender=tx.from_address,
        subject=subject,
        ctx=ctx,
        mint_source=ZERO_ADDRESS,
    )
    events = finalize(drafts)
    claimed = {i for e in events for i in e.claimed_flow_ids}
    return DecodedTx(
        chain=ctx.chain,
        tx_hash=tx.tx_hash,
        subject_wallet=subject,
        succeeded=receipt.status != 0,
        events=events,
        flows=ledger.flows,
        unclaimed_flow_ids=tuple(f.flow_id for f in ledger.flows if f.flow_id not in claimed),
        warnings=tuple(ledger.warnings),
    )


def _upgrade_valuable(event: NormalizedEvent, ctx: DecodeContext) -> NormalizedEvent:
    """T2 事件的持仓能按家族估值时升到 T3（设计文档 3.3：可估值）。交换这类没有持仓键的事件停在 T2。"""
    if event.coverage_tier is CoverageTier.T2 and ctx.is_valuable(event.instance_key, event.position_key):
        return replace(event, coverage_tier=CoverageTier.T3)
    return event


def _approval_events(approvals: Sequence[Approval], tx: TxInfo, subject: str, ctx: DecodeContext) -> list[EventDraft]:
    drafts = []
    for a in approvals:
        # 别人发起的交易里也会出现钱包作为 owner 的 Approval：transferFrom 更新授权额度（地址投毒样本里
        # 就有额度被改成 0 的记录），或者别人代为提交钱包签名的 permit。不能丢掉，但要标明不是钱包发起的。
        extra = {"spender": a.spender, "initiated_by_subject": tx.from_address == subject}
        extra.update(
            {
                k: v
                for k, v in (("amount_raw", a.amount_raw), ("token_id", a.token_id), ("approved", a.approved))
                if v is not None
            }
        )
        drafts.append(
            informational_event(
                ctx=ctx,
                tx_hash=tx.tx_hash,
                subject=subject,
                subtype=EventSubtype.APPROVE,
                log_index=a.log_index,
                asset=a.token,
                counterparty=a.spender,
                extra=extra,
            )
        )
    return drafts
