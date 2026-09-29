"""第一段通用解码的 EVM 实现：从交易和回执里提取主体钱包参与的资产流水和授权（纯函数）。

只提取 from 或 to 等于主体钱包的流水：钱包视角之外的转账（例如聚合器路由内部在池子之间的搬运）
不影响钱包的资产，家族需要时直接读回执日志。不看金额大小，不丢弃任何金额（设计文档 G3）。

流水编号（flow_id）的顺序，同一输入稳定：
1. 系统交易铸造的原生币（OP Stack 存款交易的 mint）；
2. 交易 value；
3. 日志里的转账，按 log_index 升序（ERC1155 批量转账按数组顺序展开）；
4. 数据源给出的内部交易，按数据源给出的顺序；
5. gas。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from alpha_core.chain_data import RawLog, TxInfo, TxReceipt
from eth_abi import decode as abi_decode
from eth_utils import keccak

from ..models import GAS_SINK, NATIVE, SYSTEM_SOURCE, AssetFlow, AssetFlowKind, FlowSource, InternalTransfer
from .rules import ChainRules, gas_paid, is_op_deposit


def event_topic(signature: str) -> str:
    """事件签名的 topic0（小写、带 0x）。"""
    return "0x" + keccak(text=signature).hex()


TRANSFER = event_topic("Transfer(address,address,uint256)")  # ERC20（3 个 topic）和 ERC721（4 个 topic）同签名
TRANSFER_SINGLE = event_topic("TransferSingle(address,address,address,uint256,uint256)")
TRANSFER_BATCH = event_topic("TransferBatch(address,address,address,uint256[],uint256[])")
APPROVAL = event_topic("Approval(address,address,uint256)")  # ERC20（3 个 topic）和 ERC721（4 个 topic）同签名
APPROVAL_FOR_ALL = event_topic("ApprovalForAll(address,address,bool)")

# 已由本模块处理的 token 标准事件，通用 ABI 解码不再重复解它们
TOKEN_STANDARD_TOPICS = frozenset({TRANSFER, TRANSFER_SINGLE, TRANSFER_BATCH, APPROVAL, APPROVAL_FOR_ALL})


@dataclass(frozen=True)
class Approval:
    """主体钱包作为 owner 的一次授权。"""

    token: str  # 被授权的 token 合约
    spender: str  # 被授权方（ERC721 单个授权为 approved 地址，ApprovalForAll 为 operator）
    log_index: int
    amount_raw: int | None = None  # ERC20 授权额度；其他标准为 None
    token_id: int | None = None  # ERC721 单个 tokenId 的授权
    approved: bool | None = None  # ApprovalForAll 的开关


@dataclass(frozen=True)
class ExtractedTx:
    """第一段的产出。"""

    flows: tuple[AssetFlow, ...]
    approvals: tuple[Approval, ...]
    malformed_log_indexes: tuple[int, ...]  # 签名是转账/授权但格式不符合标准、无法解析的日志


def _addr(topic: str) -> str:
    return "0x" + topic[-40:]


def _word(data: str, i: int) -> int | None:
    raw = data.removeprefix("0x")
    chunk = raw[64 * i : 64 * (i + 1)]
    return int(chunk, 16) if len(chunk) == 64 else None


class _Builder:
    def __init__(self) -> None:
        self.flows: list[AssetFlow] = []

    def add(self, kind: AssetFlowKind, asset: str, amount: int, frm: str, to: str, source: FlowSource, **kw) -> None:
        self.flows.append(AssetFlow(len(self.flows), kind, asset, amount, frm, to, source, **kw))


def _log_flows(log: RawLog, subject: str, b: _Builder) -> bool:
    """把一条转账日志里和主体有关的部分加进流水；返回日志是否格式正确（不是转账日志也算正确）。"""
    t = log.topics
    if not t or t[0] not in (TRANSFER, TRANSFER_SINGLE, TRANSFER_BATCH):
        return True
    if t[0] == TRANSFER and len(t) == 3:
        amount = _word(log.data, 0)
        if amount is None:
            return False
        frm, to = _addr(t[1]), _addr(t[2])
        if subject in (frm, to):
            b.add(AssetFlowKind.ERC20, log.address, amount, frm, to, FlowSource.LOG, log_index=log.log_index)
        return True
    if t[0] == TRANSFER and len(t) == 4:
        frm, to = _addr(t[1]), _addr(t[2])
        if subject in (frm, to):
            b.add(
                AssetFlowKind.ERC721,
                log.address,
                1,
                frm,
                to,
                FlowSource.LOG,
                token_id=int(t[3], 16),
                log_index=log.log_index,
            )
        return True
    if t[0] in (TRANSFER_SINGLE, TRANSFER_BATCH) and len(t) == 4:
        frm, to = _addr(t[2]), _addr(t[3])
        if subject not in (frm, to):
            return True
        try:
            if t[0] == TRANSFER_SINGLE:
                ids, values = abi_decode(["uint256", "uint256"], bytes.fromhex(log.data[2:]))
                pairs = [(ids, values)]
            else:
                ids, values = abi_decode(["uint256[]", "uint256[]"], bytes.fromhex(log.data[2:]))
                pairs = list(zip(ids, values, strict=True))
        except Exception:  # noqa: BLE001 - 非标准实现的日志，按格式错误上报，不中断解码
            return False
        for token_id, value in pairs:
            b.add(
                AssetFlowKind.ERC1155,
                log.address,
                value,
                frm,
                to,
                FlowSource.LOG,
                token_id=token_id,
                log_index=log.log_index,
            )
        return True
    return False  # 签名对得上但 topic 个数不对


def _approval(log: RawLog, subject: str) -> Approval | None | bool:
    """解析主体作为 owner 的授权；不是授权日志返回 None，格式错误返回 False。"""
    t = log.topics
    if not t or t[0] not in (APPROVAL, APPROVAL_FOR_ALL) or len(t) < 3 or _addr(t[1]) != subject:
        return None
    if t[0] == APPROVAL and len(t) == 3:
        amount = _word(log.data, 0)
        if amount is None:
            return False
        return Approval(log.address, _addr(t[2]), log.log_index, amount_raw=amount)
    if t[0] == APPROVAL and len(t) == 4:
        return Approval(log.address, _addr(t[2]), log.log_index, token_id=int(t[3], 16))
    if t[0] == APPROVAL_FOR_ALL and len(t) == 3:
        flag = _word(log.data, 0)
        if flag is None:
            return False
        return Approval(log.address, _addr(t[2]), log.log_index, approved=bool(flag))
    return False


def extract(
    tx: TxInfo,
    receipt: TxReceipt,
    subject: str,
    rules: ChainRules,
    internal: Sequence[InternalTransfer] | None = None,
) -> ExtractedTx:
    """提取主体钱包参与的资产流水和授权。

    @param subject 主体钱包，小写
    @param rules 链画像声明的 gas 模型和系统交易规则
    @param internal 数据源给出的内部交易；None 表示数据源不可用（与"确实没有内部交易"的空列表不同）
    @raises ValueError 计算 gas 缺少必要字段
    """
    subject = subject.lower()
    b = _Builder()
    succeeded = receipt.status != 0
    sender = tx.from_address
    recipient = tx.to_address or receipt.contract_address or ""

    if is_op_deposit(tx, rules) and tx.mint and sender == subject:
        b.add(AssetFlowKind.NATIVE, NATIVE, tx.mint, SYSTEM_SOURCE, sender, FlowSource.SYSTEM)
    if succeeded and tx.value and subject in (sender, recipient):
        b.add(AssetFlowKind.NATIVE, NATIVE, tx.value, sender, recipient, FlowSource.TX)

    approvals: list[Approval] = []
    malformed: list[int] = []
    if succeeded:
        for log in sorted(receipt.logs, key=lambda lg: lg.log_index):
            if not _log_flows(log, subject, b):
                malformed.append(log.log_index)
            got = _approval(log, subject)
            if got is False:
                malformed.append(log.log_index)
            elif got is not None:
                approvals.append(got)
        for item in internal or ():
            if subject in (item.from_address, item.to_address) and item.amount_raw > 0:
                b.add(
                    AssetFlowKind.NATIVE,
                    NATIVE,
                    item.amount_raw,
                    item.from_address,
                    item.to_address,
                    FlowSource.INTERNAL,
                )

    if sender == subject:
        fee = gas_paid(tx, receipt, rules)
        if fee:
            b.add(AssetFlowKind.GAS, NATIVE, fee, sender, GAS_SINK, FlowSource.TX)
    return ExtractedTx(tuple(b.flows), tuple(approvals), tuple(malformed))
