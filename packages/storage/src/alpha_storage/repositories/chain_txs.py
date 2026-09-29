"""交易与回执日志仓储（`chain_txs` + `chain_logs`）。回执和日志不可变，写入后永不重拉。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from alpha_core.chain_data import RawLog, TxReceipt
from alpha_core.types import Chain
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import ChainLogRow, ChainTxRow


@dataclass(frozen=True)
class TxRecord:
    """一笔交易的基本信息（通常来自地址索引源）；未知的字段为 None。"""

    tx_hash: str  # 小写带 0x
    block_number: int
    from_address: str
    to_address: str | None  # 创建合约的交易为 None
    value_raw: int = 0  # 原生币数量（wei）
    method_selector: str | None = None
    tx_index: int | None = None
    status: int | None = None  # 1 成功 / 0 失败 / None 未知
    gas_used: int | None = None
    effective_gas_price: int | None = None
    input_data: str | None = None  # 完整调用数据；只有选择器时不填
    tx_type: int | None = None  # EIP-2718 交易类型
    mint_raw: int | None = None  # OP Stack 存款交易铸造的原生币（wei）


class ChainTxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_txs(self, chain: Chain, txs: list[TxRecord], *, source: str) -> None:
        """写入交易；已存在的交易只补全原来为空的字段（不同来源给的字段不一样，互相补齐）。"""
        if not txs:
            return
        rows = [
            {
                "chain": chain.value,
                "tx_hash": t.tx_hash.lower(),
                "block_number": t.block_number,
                "tx_index": t.tx_index,
                "from_address": t.from_address.lower(),
                "to_address": t.to_address.lower() if t.to_address else None,
                "value_raw": Decimal(t.value_raw),
                "method_selector": t.method_selector,
                "status": t.status,
                "gas_used": t.gas_used,
                "effective_gas_price": None if t.effective_gas_price is None else Decimal(t.effective_gas_price),
                "input_data": t.input_data.lower() if t.input_data else None,
                "tx_type": t.tx_type,
                "mint_raw": None if t.mint_raw is None else Decimal(t.mint_raw),
                "source": source,
            }
            for t in {t.tx_hash.lower(): t for t in txs}.values()
        ]
        stmt = insert(ChainTxRow).values(rows)
        ex = stmt.excluded
        fill = (
            "tx_index",
            "to_address",
            "method_selector",
            "status",
            "gas_used",
            "effective_gas_price",
            "input_data",
            "tx_type",
            "mint_raw",
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["chain", "tx_hash"],
            set_={c: func.coalesce(getattr(ChainTxRow, c), getattr(ex, c)) for c in fill},
        )
        self._session.execute(stmt)

    def save_receipts(self, chain: Chain, receipts: list[TxReceipt]) -> None:
        """写入回执及其日志，并把 `receipt_fetched` 置为 true；应当在同一个事务里调用。

        回执里的字段是权威值，覆盖索引源给的同名字段。
        """
        if not receipts:
            return
        self.upsert_txs(
            chain,
            [
                TxRecord(
                    tx_hash=r.tx_hash,
                    block_number=r.block_number,
                    from_address=r.from_address,
                    to_address=r.to_address,
                    tx_index=r.tx_index,
                )
                for r in receipts
            ],
            source="rpc",
        )
        for r in receipts:
            self._session.execute(
                update(ChainTxRow)
                .where(ChainTxRow.chain == chain.value, ChainTxRow.tx_hash == r.tx_hash)
                .values(
                    status=r.status,
                    gas_used=r.gas_used,
                    effective_gas_price=None if r.effective_gas_price is None else Decimal(r.effective_gas_price),
                    contract_address=r.contract_address,
                    tx_index=r.tx_index,
                    l1_fee=None if r.l1_fee is None else Decimal(r.l1_fee),
                    receipt_fetched=True,
                )
            )
        logs = [log for r in receipts for log in r.logs]
        if logs:
            stmt = insert(ChainLogRow).values([_log_row(chain, log) for log in logs])
            self._session.execute(stmt.on_conflict_do_nothing(index_elements=["chain", "tx_hash", "log_index"]))

    def list_missing_receipts(self, chain: Chain, tx_hashes: list[str]) -> list[str]:
        """返回给定交易中还没有回执的（包括库里根本没有的），保持输入顺序。"""
        wanted = list(dict.fromkeys(h.lower() for h in tx_hashes))
        if not wanted:
            return []
        fetched = set(
            self._session.scalars(
                select(ChainTxRow.tx_hash).where(
                    ChainTxRow.chain == chain.value,
                    ChainTxRow.tx_hash.in_(wanted),
                    ChainTxRow.receipt_fetched.is_(True),
                )
            )
        )
        return [h for h in wanted if h not in fetched]

    def get_logs(self, chain: Chain, tx_hashes: list[str]) -> dict[str, list[RawLog]]:
        """按交易读取已入库的日志，每笔交易内按 log_index 升序。"""
        wanted = {h.lower() for h in tx_hashes}
        if not wanted:
            return {}
        rows = self._session.scalars(
            select(ChainLogRow)
            .where(ChainLogRow.chain == chain.value, ChainLogRow.tx_hash.in_(wanted))
            .order_by(ChainLogRow.tx_hash, ChainLogRow.log_index)
        )
        out: dict[str, list[RawLog]] = {}
        for row in rows:
            topics = [t for t in (row.topic0, row.topic1, row.topic2, row.topic3) if t is not None]
            out.setdefault(row.tx_hash, []).append(
                RawLog(
                    address=row.address,
                    topics=topics,
                    data=row.data,
                    log_index=row.log_index,
                    block_number=row.block_number,
                    tx_hash=row.tx_hash,
                )
            )
        return out


def _log_row(chain: Chain, log: RawLog) -> dict:
    topics = list(log.topics) + [None] * (4 - len(log.topics))
    return {
        "chain": chain.value,
        "tx_hash": log.tx_hash,
        "log_index": log.log_index,
        "block_number": log.block_number,
        "address": log.address,
        "topic0": topics[0],
        "topic1": topics[1],
        "topic2": topics[2],
        "topic3": topics[3],
        "data": log.data,
    }
