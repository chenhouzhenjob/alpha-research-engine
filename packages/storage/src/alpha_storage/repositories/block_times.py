"""区块时间仓储；按结构实现 `alpha_core.ports.BlockTimeStore`。"""

from __future__ import annotations

from datetime import datetime

from alpha_core.ports import BlockTimeSource
from alpha_core.types import Chain
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import BlockTimeRow


class BlockTimeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_many(self, chain: Chain, block_numbers: list[int]) -> dict[int, datetime]:
        """只返回库里已有的区块；缺失的不出现在结果里。"""
        if not block_numbers:
            return {}
        rows = self._session.execute(
            select(BlockTimeRow.block_number, BlockTimeRow.block_time).where(
                BlockTimeRow.chain == chain.value, BlockTimeRow.block_number.in_(set(block_numbers))
            )
        )
        return {number: ts for number, ts in rows}

    def put_many(self, chain: Chain, times: dict[int, datetime], source: BlockTimeSource) -> None:
        """批量写入；区块时间不可变，已存在的保留原值。"""
        if not times:
            return
        stmt = insert(BlockTimeRow).values(
            [
                {"chain": chain.value, "block_number": b, "block_time": t, "source": source.value}
                for b, t in times.items()
            ]
        )
        self._session.execute(stmt.on_conflict_do_nothing(index_elements=["chain", "block_number"]))
