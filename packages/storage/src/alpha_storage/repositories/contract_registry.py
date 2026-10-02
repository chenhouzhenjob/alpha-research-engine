"""合约识别结果仓储（`contract_registry`）。"""

from __future__ import annotations

from alpha_core.ports import ContractRecord, ReviewStatus
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import ContractRegistryRow

_UPDATABLE = (
    "kind",
    "family",
    "instance_key",
    "code_hash",
    "implementation_address",
    "source",
    "confidence",
    "review_status",
    "evidence",
)


class ContractRegistryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_many(self, chain: str, addresses: list[str]) -> dict[str, ContractRecord]:
        wanted = list(dict.fromkeys(a.lower() for a in addresses))
        if not wanted:
            return {}
        rows = self._session.scalars(
            select(ContractRegistryRow).where(
                ContractRegistryRow.chain == chain, ContractRegistryRow.address.in_(wanted)
            )
        )
        return {
            r.address: ContractRecord(
                chain=r.chain,
                address=r.address,
                kind=r.kind,
                source=r.source,
                family=r.family,
                instance_key=r.instance_key,
                code_hash=r.code_hash,
                implementation_address=r.implementation_address,
                confidence=r.confidence,
                review_status=ReviewStatus(r.review_status),
                evidence=dict(r.evidence or {}),
            )
            for r in rows
        }

    def upsert_many(self, records: list[ContractRecord]) -> None:
        """写入识别结果。已是 confirmed（人工确认）的行保持不变，其余按新结果覆盖。"""
        if not records:
            return
        rows = [
            {
                "chain": r.chain,
                "address": r.address.lower(),
                "kind": r.kind,
                "family": r.family,
                "instance_key": r.instance_key,
                "code_hash": r.code_hash,
                "implementation_address": r.implementation_address,
                "source": r.source,
                "confidence": r.confidence,
                "review_status": r.review_status.value,
                "evidence": r.evidence,
            }
            for r in {(r.chain, r.address.lower()): r for r in records}.values()
        ]
        stmt = insert(ContractRegistryRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["chain", "address"],
            set_={**{c: getattr(stmt.excluded, c) for c in _UPDATABLE}, "updated_at": func.now()},
            where=ContractRegistryRow.review_status != ReviewStatus.CONFIRMED.value,
        )
        self._session.execute(stmt)
