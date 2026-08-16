"""ERC20 token 元数据缓存仓储（目前只有 decimals，见 `TokenRow` 的注释）。"""

from __future__ import annotations

from datetime import UTC, datetime

from alpha_core.types import Chain
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import TokenRow


class TokenRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_decimals(self, chain: Chain, token_address: str) -> int | None:
        """`None` 表示还没缓存过，调用方需要自己去链上查一次再调 `upsert`。"""
        row = self._session.scalar(
            select(TokenRow).where(TokenRow.chain == chain.value, TokenRow.token_address == token_address)
        )
        return row.decimals if row else None

    def upsert(self, chain: Chain, token_address: str, decimals: int) -> None:
        stmt = insert(TokenRow).values(
            chain=chain.value,
            token_address=token_address,
            decimals=decimals,
            fetched_at=datetime.now(UTC),
        )
        # decimals 不可变，理论上不会真的冲突覆盖出不同值；用 do_nothing 而不是 do_update，
        # 冲突时保留已有行，语义上更贴近"这是永久缓存，不是可刷新的快照"。
        stmt = stmt.on_conflict_do_nothing(index_elements=["chain", "token_address"])
        self._session.execute(stmt)
