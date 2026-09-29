"""EVM 交易的解码入口（纯函数）。

当前流程（步骤 3）：
1. `flows.extract`：提取主体钱包参与的资产流水和授权；
2. （步骤 5 在这里插入第二段分派：家族认领流水、产出带协议语义的事件）；
3. 兜底：未认领的流水转成 transfer / receive / fee / bridge 事件；授权转成 informational/approve；
4. 通用 ABI 解码：主体钱包交互范围内、未识别合约发出的日志，解成 informational/decoded_log。

"交互范围"指：日志由交易的 `to` 发出，或日志的 topic 里带着主体钱包。聚合器路由内部在池子之间
搬运产生的大量日志不在范围内，避免一笔交易产出几百条无意义的事件。
"""

from __future__ import annotations

from collections.abc import Sequence

from alpha_core.chain_data import RawLog, TxInfo, TxReceipt

from ..context import DecodeContext
from ..events import EventDraft, finalize
from ..fallback import fallback_events, informational_event
from ..models import DecodedTx, DecodeWarning, EventSubtype, InternalTransfer, WarningCode
from .abi_logs import decode_with_contract_abi, decode_with_signature
from .flows import TOKEN_STANDARD_TOPICS, extract
from .rules import ChainRules

ZERO_ADDRESS = "0x" + "0" * 40  # EVM 上 token 铸币的来源地址


def _in_scope(log: RawLog, tx: TxInfo, subject: str) -> bool:
    wallet_topic = "0x" + "0" * 24 + subject[2:]
    return log.address == (tx.to_address or "") or wallet_topic in log.topics[1:]


def _decode_unknown_logs(
    receipt: TxReceipt, tx: TxInfo, subject: str, ctx: DecodeContext
) -> tuple[list[EventDraft], list[str], list[DecodeWarning]]:
    drafts: list[EventDraft] = []
    unknown: dict[str, None] = {}
    missing: dict[str, None] = {}
    for log in sorted(receipt.logs, key=lambda lg: lg.log_index):
        if not log.topics or log.topics[0] in TOKEN_STANDARD_TOPICS or not _in_scope(log, tx, subject):
            continue
        if log.address in ctx.identities or log.address in ctx.tokens:
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


def decode_generic(
    tx: TxInfo,
    receipt: TxReceipt,
    subject: str,
    ctx: DecodeContext,
    rules: ChainRules,
    internal: Sequence[InternalTransfer] | None = None,
) -> DecodedTx:
    """解码一笔 EVM 交易在主体钱包视角下的资产流动（T0）。

    @param internal 数据源给出的内部交易；None 表示数据源不可用
    @raises ValueError 计算 gas 缺少必要字段
    @raises TaxonomyError 产出的事件不符合分类表（说明规则有 bug）
    """
    subject = subject.lower()
    extracted = extract(tx, receipt, subject, rules, internal)
    drafts = fallback_events(
        extracted.flows,
        tx_hash=tx.tx_hash,
        tx_sender=tx.from_address,
        subject=subject,
        ctx=ctx,
        mint_source=ZERO_ADDRESS,
    )
    for a in extracted.approvals:
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
    decoded, unknown, warnings = (
        _decode_unknown_logs(receipt, tx, subject, ctx) if receipt.status != 0 else ([], [], [])
    )
    drafts += decoded
    warnings += [
        DecodeWarning(WarningCode.MALFORMED_LOG, f"日志 {i} 格式不符合标准") for i in extracted.malformed_log_indexes
    ]
    return DecodedTx(
        chain=ctx.chain,
        tx_hash=tx.tx_hash,
        subject_wallet=subject,
        succeeded=receipt.status != 0,
        events=finalize(drafts),
        flows=extracted.flows,
        unclaimed_flow_ids=(),
        unknown_contracts=tuple(unknown),
        warnings=tuple(warnings),
    )
