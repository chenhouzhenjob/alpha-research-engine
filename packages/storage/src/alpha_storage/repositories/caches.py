"""ABI、历史价格、可变状态三类缓存的仓储；分别按结构实现 `alpha_core.ports` 里的
`AbiStore`、`PriceStore`、`StateCache`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from alpha_core.ports import (
    AbiEntry,
    AbiKeyType,
    AbiStatus,
    CachedState,
    PriceConfidence,
    PriceGranularity,
    PricePoint,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import AbiCacheRow, ChainStateCacheRow, PricePointRow


class AbiCacheRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, chain: str, key_type: AbiKeyType, key: str) -> AbiEntry | None:
        row = self._session.get(AbiCacheRow, (chain, key_type.value, key.lower()))
        if row is None:
            return None
        return AbiEntry(
            chain=row.chain,
            key_type=AbiKeyType(row.key_type),
            key=row.key,
            status=AbiStatus(row.status),
            source=row.source,
            name=row.name,
            abi=row.abi,
            fetched_at=row.fetched_at,
            retry_after=row.retry_after,
        )

    def put(self, entry: AbiEntry) -> None:
        values = {
            "chain": entry.chain,
            "key_type": entry.key_type.value,
            "key": entry.key.lower(),
            "status": entry.status.value,
            "source": entry.source,
            "name": entry.name,
            "abi": entry.abi,
            "fetched_at": entry.fetched_at,
            "retry_after": entry.retry_after,
        }
        stmt = insert(AbiCacheRow).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["chain", "key_type", "key"],
            set_={k: v for k, v in values.items() if k not in ("chain", "key_type", "key")},
        )
        self._session.execute(stmt)


class PricePointRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self, chain: str, token_address: str, granularity: PriceGranularity, bucket_start: datetime
    ) -> PricePoint | None:
        row = self._session.get(PricePointRow, (chain, token_address.lower(), granularity.value, bucket_start))
        if row is None:
            return None
        return PricePoint(
            chain=row.chain,
            token_address=row.token_address,
            granularity=PriceGranularity(row.granularity),
            bucket_start=row.bucket_start,
            price_usd=row.price_usd,
            source=row.source,
            source_ref=row.source_ref,
            confidence=PriceConfidence(row.confidence),
            reference_price_usd=row.reference_price_usd,
        )

    def put(self, point: PricePoint) -> None:
        """写入一个已收盘时间桶的价格；已有记录保留（过去的价格不变）。"""
        stmt = insert(PricePointRow).values(
            chain=point.chain,
            token_address=point.token_address.lower(),
            granularity=point.granularity.value,
            bucket_start=point.bucket_start,
            price_usd=point.price_usd,
            source=point.source,
            source_ref=point.source_ref,
            confidence=point.confidence.value,
            reference_price_usd=point.reference_price_usd,
            fetched_at=datetime.now(UTC),
        )
        self._session.execute(
            stmt.on_conflict_do_nothing(index_elements=["chain", "token_address", "granularity", "bucket_start"])
        )


class ChainStateCacheRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, chain: str, key: str, *, max_age_seconds: float) -> CachedState | None:
        """超过 `max_age_seconds` 的记录视为不存在。"""
        row = self._session.scalar(
            select(ChainStateCacheRow).where(
                ChainStateCacheRow.chain == chain,
                ChainStateCacheRow.key == key,
                ChainStateCacheRow.fetched_at >= datetime.now(UTC) - timedelta(seconds=max_age_seconds),
            )
        )
        if row is None:
            return None
        return CachedState(value=row.value, block_number=row.block_number, fetched_at=row.fetched_at)

    def put(self, chain: str, key: str, value: Any, *, block_number: int | None) -> None:
        values = {
            "chain": chain,
            "key": key,
            "value": value,
            "block_number": block_number,
            "fetched_at": datetime.now(UTC),
        }
        stmt = insert(ChainStateCacheRow).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["chain", "key"],
            set_={"value": value, "block_number": block_number, "fetched_at": values["fetched_at"]},
        )
        self._session.execute(stmt)
