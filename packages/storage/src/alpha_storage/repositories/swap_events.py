"""逐笔 Swap 事件仓储。"""

from __future__ import annotations

from alpha_core.models import SwapEvent
from alpha_core.types import Chain
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import SwapEventRow


class SwapEventRepository:
    """`swap_events` 的读写仓储。按 `(chain, tx_hash, log_index)` 幂等写入——同一笔交易可能被
    订阅进程和断线回填的 `get_logs` 都拉到一次，冲突时什么都不做（不可变数据只落一次，
    和 `pool_metrics_history` 之外的另一种"不可变"语义：这里连覆盖更新都不需要）。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, events: list[SwapEvent]) -> None:
        if not events:
            return
        rows = [
            {
                "chain": e.chain.value,
                "pool_address": e.pool_address,
                "instrument_id": e.instrument_id,
                "tx_hash": e.tx_hash,
                "log_index": e.log_index,
                "block_number": e.block_number,
                "sender": e.sender,
                "recipient": e.recipient,
                "amount0": e.amount0,
                "amount1": e.amount1,
                "sqrt_price_x96_after": e.sqrt_price_x96_after,
                "tick_after": e.tick_after,
                "fetched_at": e.fetched_at,
                "block_time": e.block_time,
            }
            for e in events
        ]
        stmt = insert(SwapEventRow).values(rows)
        stmt = stmt.on_conflict_do_nothing(constraint="uq_swap_events_chain_tx_log")
        self._session.execute(stmt)

    def get_since_block(
        self, chain: Chain, pool_address: str, from_block: int
    ) -> list[SwapEventRow]:
        """返回指定池子从 `from_block`（含）起的全部事件，按区块号、log_index 升序，供 K 线聚合消费。"""
        stmt = (
            select(SwapEventRow)
            .where(
                SwapEventRow.chain == chain.value,
                SwapEventRow.pool_address == pool_address,
                SwapEventRow.block_number >= from_block,
            )
            .order_by(SwapEventRow.block_number.asc(), SwapEventRow.log_index.asc())
        )
        return list(self._session.scalars(stmt))

    def get_max_block_number(self, chain: Chain, pool_address: str) -> int | None:
        """返回指定池子已落库事件里最大的 `block_number`，`None` 表示还没有任何事件。

        订阅进程重连后，用这个值（+1）作为 `get_logs` 回填的起点——`swap_events` 本身就是
        水位线，不需要像 Factory 全量扫描（`chain_cursors`）那样另建一张游标表：那张表的键是
        `(chain, dex_id)`，是"整个 DEX 扫到哪了"的语义，跟这里"某个池子订阅到哪了"是不同粒度，
        不适合复用同一张表、同一个键结构。
        """
        stmt = select(func.max(SwapEventRow.block_number)).where(
            SwapEventRow.chain == chain.value, SwapEventRow.pool_address == pool_address
        )
        return self._session.scalar(stmt)
