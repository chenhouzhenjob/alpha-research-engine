"""候选池目录仓储。"""

from __future__ import annotations

from datetime import UTC, datetime

from alpha_core.models import PoolCandidate
from alpha_core.types import Chain, PoolCandidateStatus
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import PoolCandidateRow


class PoolCandidateRepository:
    """候选池的读写仓储。新发现的池子按 (chain, pool_address) 幂等写入，不重复插入。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, candidates: list[PoolCandidate]) -> None:
        """批量幂等写入。已存在的池子只保留首次发现记录，不覆盖 status（可能已被人工/打分流程推进过）。"""
        if not candidates:
            return
        now = datetime.now(UTC)
        rows = [
            {
                "chain": c.chain.value,
                "dex_id": c.dex_id.value,
                "pool_address": c.pool_address,
                "token0_address": c.token0_address,
                "token1_address": c.token1_address,
                "fee_pips": c.fee_pips,
                "tick_spacing": c.tick_spacing,
                "created_at_block": c.created_at_block,
                "created_at": c.created_at,
                "status": c.status.value,
                "discovered_at": now,
                "updated_at": now,
            }
            for c in candidates
        ]
        stmt = insert(PoolCandidateRow).values(rows)
        stmt = stmt.on_conflict_do_nothing(constraint="uq_pool_candidates_chain_pool")
        self._session.execute(stmt)

    def list_by_status(
        self, chain: Chain, status: PoolCandidateStatus
    ) -> list[PoolCandidateRow]:
        stmt = select(PoolCandidateRow).where(
            PoolCandidateRow.chain == chain.value, PoolCandidateRow.status == status.value
        )
        return list(self._session.scalars(stmt))

    def list_all(self, chain: Chain) -> list[PoolCandidateRow]:
        stmt = select(PoolCandidateRow).where(PoolCandidateRow.chain == chain.value)
        return list(self._session.scalars(stmt))

    def get_by_address(self, chain: Chain, pool_address: str) -> PoolCandidateRow | None:
        stmt = select(PoolCandidateRow).where(
            PoolCandidateRow.chain == chain.value, PoolCandidateRow.pool_address == pool_address
        )
        return self._session.scalar(stmt)

    def update_status(self, chain: Chain, pool_address: str, status: PoolCandidateStatus) -> None:
        row = self._session.scalar(
            select(PoolCandidateRow).where(
                PoolCandidateRow.chain == chain.value,
                PoolCandidateRow.pool_address == pool_address,
            )
        )
        if row is not None:
            row.status = status.value
            row.updated_at = datetime.now(UTC)
