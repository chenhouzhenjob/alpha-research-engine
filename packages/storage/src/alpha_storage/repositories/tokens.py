"""ERC20/NFT token 元数据缓存仓储（decimals、symbol、name 等，见 `TokenRow` 的注释）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from alpha_core.types import Chain
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import TokenRow


@dataclass(frozen=True)
class TokenRecord:
    """token 元数据的一条记录；读不出来的字段为 None。"""

    token_address: str  # 小写
    decimals: int  # NFT 记 0
    symbol: str | None
    name: str | None
    standard: str | None  # erc20/erc721/erc1155；老数据为 None，按 erc20 理解
    risk_flag: str  # normal/spam/impersonator/hacked


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

    def get_many(self, chain: Chain, token_addresses: list[str]) -> dict[str, TokenRecord]:
        """批量读取；返回值的键是调用方传入地址的小写形式，缺失的不出现在结果里。

        `token_address` 列是历史遗留的 CHAR(42)，地址恰好 42 位不会被补空格，但仍按
        去空格、转小写后的值匹配，不依赖数据库返回的原始字符串。
        """
        wanted = {a.lower() for a in token_addresses}
        if not wanted:
            return {}
        rows = self._session.scalars(
            select(TokenRow).where(TokenRow.chain == chain.value, TokenRow.token_address.in_(wanted))
        )
        out: dict[str, TokenRecord] = {}
        for row in rows:
            addr = row.token_address.strip().lower()
            out[addr] = TokenRecord(
                token_address=addr,
                decimals=row.decimals,
                symbol=row.symbol,
                name=row.name,
                standard=row.standard,
                risk_flag=row.risk_flag,
            )
        return out

    def upsert_metadata(
        self,
        chain: Chain,
        token_address: str,
        *,
        decimals: int,
        symbol: str | None,
        name: str | None,
        standard: str | None,
        source: str,
    ) -> None:
        """写入或补全元数据：decimals 不可变，已有值不覆盖；symbol/name/standard 只在原来为空时补上。"""
        now = datetime.now(UTC)
        stmt = insert(TokenRow).values(
            chain=chain.value,
            token_address=token_address.lower(),
            decimals=decimals,
            fetched_at=now,
            symbol=symbol,
            name=name,
            standard=standard,
            source=source,
            updated_at=now,
        )
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["chain", "token_address"],
            set_={
                "symbol": func.coalesce(TokenRow.symbol, excluded.symbol),
                "name": func.coalesce(TokenRow.name, excluded.name),
                "standard": func.coalesce(TokenRow.standard, excluded.standard),
                "updated_at": now,
            },
        )
        self._session.execute(stmt)
