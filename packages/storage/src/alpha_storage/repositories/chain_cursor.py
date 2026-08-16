"""Factory 扫描水位线仓储：支持增量扫描，避免每次全量重扫全链历史。"""

from __future__ import annotations

from datetime import UTC, datetime

from alpha_core.types import Chain, DexId
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import ChainCursorRow


class ChainCursorRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_last_scanned_block(self, chain: Chain, dex_id: DexId) -> int | None:
        """返回水位线；`None` 表示该 (chain, dex) 从未扫描过，调用方需要自行决定扫描起点。"""
        row = self._session.scalar(
            select(ChainCursorRow).where(
                ChainCursorRow.chain == chain.value, ChainCursorRow.dex_id == dex_id.value
            )
        )
        return row.last_scanned_block if row else None

    def advance(self, chain: Chain, dex_id: DexId, last_scanned_block: int) -> None:
        """把水位线推进到 `last_scanned_block`（调用方必须保证这是一个已终结区块，见链重组约束）。"""
        stmt = insert(ChainCursorRow).values(
            chain=chain.value,
            dex_id=dex_id.value,
            last_scanned_block=last_scanned_block,
            updated_at=datetime.now(UTC),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_chain_cursors_chain_dex",
            set_={
                "last_scanned_block": stmt.excluded.last_scanned_block,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        self._session.execute(stmt)
