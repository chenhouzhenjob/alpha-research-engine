"""钱包登记（`wallets`）与已覆盖区块区间（`wallet_sync_ranges`）仓储。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import WalletRow, WalletSyncRangeRow


class SyncLayer(StrEnum):
    """覆盖区间的数据层：各层由不同数据源、在不同阶段补齐，覆盖范围彼此独立。"""

    TRANSFERS = "transfers"  # 地址索引源的交易和代币转账
    INTERNAL = "internal"  # 内部交易源的内部转账
    RECEIPTS = "receipts"  # 区间内需要回执的交易已全部取到回执


@dataclass(frozen=True)
class BlockRange:
    """闭区间 [from_block, to_block]。"""

    from_block: int
    to_block: int


class WalletRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def ensure(self, chain: str, address: str) -> None:
        """登记钱包；已存在时不改动（保留人工备注和首次同步时间）。"""
        stmt = insert(WalletRow).values(chain=chain, address=address.lower())
        self._session.execute(stmt.on_conflict_do_nothing(index_elements=["chain", "address"]))

    def exists(self, chain: str, address: str) -> bool:
        return self._session.get(WalletRow, (chain, address.lower())) is not None


class WalletSyncRangeRepository:
    """覆盖区间：写入时与同层重叠或相邻的区间合并，保证同层区间互不重叠、互不相邻。

    并发写入同一钱包由同步任务的钱包锁保证互斥，这里不再加行锁。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, chain: str, address: str, layer: SyncLayer, rng: BlockRange, *, source: str) -> BlockRange:
        """记录一段已覆盖区间，返回合并后的区间。

        参数 rng 应当截到数据源实际返回到的区块（不是请求的终点），否则索引源落后时会留下永久空洞。
        """
        if rng.to_block < rng.from_block:
            raise ValueError(f"区间终点早于起点：{rng}")
        address = address.lower()
        row = WalletSyncRangeRow
        touching = list(
            self._session.scalars(
                select(row).where(
                    row.chain == chain,
                    row.address == address,
                    row.layer == layer.value,
                    row.from_block <= rng.to_block + 1,
                    row.to_block >= rng.from_block - 1,
                )
            )
        )
        merged = BlockRange(
            min([rng.from_block, *(r.from_block for r in touching)]),
            max([rng.to_block, *(r.to_block for r in touching)]),
        )
        if touching:
            self._session.execute(
                delete(row).where(
                    row.chain == chain,
                    row.address == address,
                    row.layer == layer.value,
                    row.from_block.in_([r.from_block for r in touching]),
                )
            )
        self._session.add(
            WalletSyncRangeRow(
                chain=chain,
                address=address,
                layer=layer.value,
                from_block=merged.from_block,
                to_block=merged.to_block,
                source=source,
            )
        )
        self._session.flush()
        return merged

    def list(self, chain: str, address: str, layer: SyncLayer) -> list[BlockRange]:
        """该层全部已覆盖区间，按起点升序。"""
        row = WalletSyncRangeRow
        rows = self._session.execute(
            select(row.from_block, row.to_block)
            .where(row.chain == chain, row.address == address.lower(), row.layer == layer.value)
            .order_by(row.from_block)
        )
        return [BlockRange(f, t) for f, t in rows]

    def gaps(self, chain: str, address: str, layer: SyncLayer, want: BlockRange) -> list[BlockRange]:
        """want 里还没覆盖的部分，按起点升序；全部覆盖时返回空列表。"""
        return uncovered(self.list(chain, address, layer), want)


def uncovered(covered: list[BlockRange], want: BlockRange) -> list[BlockRange]:
    """纯函数：从 want 里扣掉已覆盖区间（covered 需按起点升序且互不重叠）。"""
    out: list[BlockRange] = []
    cursor = want.from_block
    for r in covered:
        if r.to_block < cursor:
            continue
        if r.from_block > want.to_block:
            break
        if r.from_block > cursor:
            out.append(BlockRange(cursor, r.from_block - 1))
        cursor = max(cursor, r.to_block + 1)
        if cursor > want.to_block:
            return out
    if cursor <= want.to_block:
        out.append(BlockRange(cursor, want.to_block))
    return out
