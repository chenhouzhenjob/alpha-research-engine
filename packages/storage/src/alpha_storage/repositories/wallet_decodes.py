"""标准事件（`wallet_events`）与交易解码摘要（`wallet_tx_decodes`）仓储。

两张表都是派生数据：一笔交易（对一个视角钱包）的事件和摘要整体替换写入，可以随时从
chain_txs + chain_logs + wallet_transfers 重新解码重建。存储层只存字符串形态的分类，
不依赖 alpha_protocols 的模型（依赖方向：protocols 不碰数据库，storage 不认识家族）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, nulls_last, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import ChainTxRow, WalletEventRow, WalletTxDecodeRow

_CHUNK = 2000  # 单条 INSERT 行数上限（本表 20 列，远低于 65535 个绑定参数）


class DecodePath(StrEnum):
    """一笔交易走的解码路径。"""

    RECEIPT = "receipt"  # 回执路径：有日志，能识别协议语义
    INDEX = "index"  # 索引路径：只凭索引源的转账解码，产出 T0 事件（第三方发起的交易）


@dataclass(frozen=True)
class WalletEventRecord:
    """一条标准事件（wallet_events 的一行，不含 chain、tx_hash、subject_wallet、block_number）。"""

    seq: int  # 交易内序号，同一输入稳定
    event_type: str  # M2 分类表的事件类型
    event_subtype: str  # M2 分类表的子类型
    direction: str  # in / out / neutral
    coverage_tier: str  # T0~T3
    confidence: str  # exact / inferred
    decoder_version: str  # <family>@<版本> 或 generic@<版本>
    asset: str | None = None  # 资产地址；原生币为 "native"；纯状态事件为 None
    amount_raw: int | None = None  # 最小单位；纯状态且无数量为 None
    token_id: int | None = None  # NFT 编号
    counterparty_address: str | None = None
    family: str | None = None  # 兜底事件为 None
    instance_key: str | None = None
    position_key: str | None = None  # 无持仓为 None
    claimed_flow_ids: tuple[int, ...] = ()  # 空表示状态事件
    extra: dict[str, Any] = field(default_factory=dict)  # 家族特有字段，须可 JSON 序列化


@dataclass(frozen=True)
class TxDecodeRecord:
    """一笔交易的解码摘要（wallet_tx_decodes 的一行，不含 chain、subject_wallet）。"""

    tx_hash: str
    block_number: int
    path: DecodePath
    succeeded: bool  # 交易是否成功
    internal_available: bool  # 解码时是否有内部交易数据
    decoder_versions: dict[str, int] = field(default_factory=dict)  # {家族或 generic: 版本}
    unclaimed_flows: int = 0  # 未认领流水条数，应为 0
    unknown_contracts: tuple[str, ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()  # ({code, detail}, …)


@dataclass(frozen=True)
class StoredEvent:
    """读出的事件：带上交易定位信息。"""

    tx_hash: str
    block_number: int
    tx_index: int | None  # 块内序号，来自 chain_txs；未知为 None
    event: WalletEventRecord


class WalletDecodeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_tx(self, chain: str, subject_wallet: str, decode: TxDecodeRecord, events: list[WalletEventRecord]) -> None:
        """整体替换一笔交易对一个视角钱包的事件和摘要；应在同一个事务里调用，保证两者一致。"""
        subject = subject_wallet.lower()
        tx_hash = decode.tx_hash.lower()
        if len({e.seq for e in events}) != len(events):
            raise ValueError(f"{tx_hash} 的事件序号重复")
        self._session.execute(
            delete(WalletEventRow).where(
                WalletEventRow.chain == chain,
                WalletEventRow.tx_hash == tx_hash,
                WalletEventRow.subject_wallet == subject,
            )
        )
        rows = [_event_row(chain, tx_hash, subject, decode.block_number, e) for e in events]
        for i in range(0, len(rows), _CHUNK):
            self._session.execute(insert(WalletEventRow).values(rows[i : i + _CHUNK]))
        summary = {
            "chain": chain,
            "tx_hash": tx_hash,
            "subject_wallet": subject,
            "block_number": decode.block_number,
            "path": decode.path.value,
            "succeeded": decode.succeeded,
            "internal_available": decode.internal_available,
            "decoder_versions": decode.decoder_versions,
            "unclaimed_flows": decode.unclaimed_flows,
            "unknown_contracts": [a.lower() for a in decode.unknown_contracts],
            "warnings": list(decode.warnings),
        }
        stmt = insert(WalletTxDecodeRow).values(summary)
        ex = stmt.excluded
        updatable = [k for k in summary if k not in ("chain", "tx_hash", "subject_wallet")]
        self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=["chain", "tx_hash", "subject_wallet"],
                set_={**{k: getattr(ex, k) for k in updatable}, "decoded_at": ex.decoded_at},
            )
        )

    def list_events(
        self,
        chain: str,
        subject_wallet: str,
        *,
        from_block: int | None = None,
        to_block: int | None = None,
        position_key: str | None = None,
    ) -> list[StoredEvent]:
        """按链上顺序返回事件：区块 → 块内序号（来自 chain_txs，未知排后）→ 交易哈希 → 交易内序号。

        持仓归组和记账依赖这个顺序：同一区块里先开仓后平仓的两笔交易不能颠倒。
        """
        ev, tx = WalletEventRow, ChainTxRow
        stmt = (
            select(ev, tx.tx_index)
            .outerjoin(tx, (tx.chain == ev.chain) & (tx.tx_hash == ev.tx_hash))
            .where(ev.chain == chain, ev.subject_wallet == subject_wallet.lower())
        )
        if from_block is not None:
            stmt = stmt.where(ev.block_number >= from_block)
        if to_block is not None:
            stmt = stmt.where(ev.block_number <= to_block)
        if position_key is not None:
            stmt = stmt.where(ev.position_key == position_key)
        stmt = stmt.order_by(ev.block_number, nulls_last(tx.tx_index.asc()), ev.tx_hash, ev.seq)
        return [
            StoredEvent(tx_hash=r.tx_hash, block_number=r.block_number, tx_index=tx_index, event=_event_record(r))
            for r, tx_index in self._session.execute(stmt)
        ]

    def list_decodes(self, chain: str, subject_wallet: str) -> list[TxDecodeRecord]:
        """钱包全部交易的解码摘要，按区块升序；用于覆盖率统计和判断解码器版本是否落后。"""
        d = WalletTxDecodeRow
        rows = self._session.scalars(
            select(d)
            .where(d.chain == chain, d.subject_wallet == subject_wallet.lower())
            .order_by(d.block_number, d.tx_hash)
        )
        return [
            TxDecodeRecord(
                tx_hash=r.tx_hash,
                block_number=r.block_number,
                path=DecodePath(r.path),
                succeeded=r.succeeded,
                internal_available=r.internal_available,
                decoder_versions=dict(r.decoder_versions or {}),
                unclaimed_flows=r.unclaimed_flows,
                unknown_contracts=tuple(r.unknown_contracts or ()),
                warnings=tuple(r.warnings or ()),
            )
            for r in rows
        ]


def _event_row(chain: str, tx_hash: str, subject: str, block_number: int, e: WalletEventRecord) -> dict:
    return {
        "chain": chain,
        "tx_hash": tx_hash,
        "seq": e.seq,
        "subject_wallet": subject,
        "block_number": block_number,
        "event_type": e.event_type,
        "event_subtype": e.event_subtype,
        "direction": e.direction,
        "asset": e.asset.lower() if e.asset else None,
        "amount_raw": None if e.amount_raw is None else Decimal(e.amount_raw),
        "token_id": None if e.token_id is None else Decimal(e.token_id),
        "counterparty_address": e.counterparty_address.lower() if e.counterparty_address else None,
        "family": e.family,
        "instance_key": e.instance_key,
        "position_key": e.position_key,
        "coverage_tier": e.coverage_tier,
        "confidence": e.confidence,
        "claimed_flow_ids": list(e.claimed_flow_ids),
        "extra": e.extra,
        "decoder_version": e.decoder_version,
    }


def _event_record(r: WalletEventRow) -> WalletEventRecord:
    return WalletEventRecord(
        seq=r.seq,
        event_type=r.event_type,
        event_subtype=r.event_subtype,
        direction=r.direction,
        coverage_tier=r.coverage_tier,
        confidence=r.confidence,
        decoder_version=r.decoder_version,
        asset=r.asset,
        amount_raw=None if r.amount_raw is None else int(r.amount_raw),
        token_id=None if r.token_id is None else int(r.token_id),
        counterparty_address=r.counterparty_address,
        family=r.family,
        instance_key=r.instance_key,
        position_key=r.position_key,
        claimed_flow_ids=tuple(r.claimed_flow_ids or ()),
        extra=dict(r.extra or {}),
    )
