"""候选池历史快照仓储。"""

from __future__ import annotations

from datetime import date

from alpha_core.models import PoolMetricsSnapshot
from alpha_core.types import Chain
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import PoolMetricsHistoryRow


class PoolMetricsRepository:
    """`pool_metrics_history` 的读写仓储。同一 (chain, pool_address, snapshot_date) 覆盖写入，
    允许重跑当天的采集任务修正数据，而不是产生重复行。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_series_up_to(
        self, chain: Chain, pool_address: str, as_of: date
    ) -> list[PoolMetricsHistoryRow]:
        """按日期升序返回 `[?, as_of]` 区间内的全部快照行，供指标计算按截止日期取历史窗口。"""
        stmt = (
            select(PoolMetricsHistoryRow)
            .where(
                PoolMetricsHistoryRow.chain == chain.value,
                PoolMetricsHistoryRow.pool_address == pool_address,
                PoolMetricsHistoryRow.snapshot_date <= as_of,
            )
            .order_by(PoolMetricsHistoryRow.snapshot_date.asc())
        )
        return list(self._session.scalars(stmt))

    def upsert_many(self, snapshots: list[PoolMetricsSnapshot]) -> None:
        if not snapshots:
            return
        rows = [
            {
                "chain": s.chain.value,
                "pool_address": s.pool_address,
                "snapshot_date": s.snapshot_date,
                "tvl_usd": s.tvl_usd,
                "volume_24h_usd": s.volume_24h_usd,
                "close_price": s.close_price,
                "data_source": s.data_source,
                "fetched_at": s.fetched_at,
            }
            for s in snapshots
        ]
        stmt = insert(PoolMetricsHistoryRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_pool_metrics_history_pool_date",
            set_={
                # 新值为 None（数据源当次没取到）时保留旧值，避免一次部分失败的重跑抹掉已有数据。
                "tvl_usd": func.coalesce(stmt.excluded.tvl_usd, PoolMetricsHistoryRow.tvl_usd),
                "volume_24h_usd": func.coalesce(
                    stmt.excluded.volume_24h_usd, PoolMetricsHistoryRow.volume_24h_usd
                ),
                "close_price": func.coalesce(
                    stmt.excluded.close_price, PoolMetricsHistoryRow.close_price
                ),
                # data_source 之前漏在 set_ 里，导致这一列一旦首次插入就再也不会更新——
                # 真实发现：接入 subgraph 之后重跑，tvl_usd/volume_24h_usd 正确从 None 变成
                # 真实值，但 data_source 一直停留在第一次插入时的 'geckoterminal'，没有反映出
                # 真正的数据来源。跟上面几个数值字段不同，这里不需要 COALESCE——data_source
                # 每条路径都会给一个真实字符串，不会是 None，直接覆盖。
                "data_source": stmt.excluded.data_source,
                "fetched_at": stmt.excluded.fetched_at,
            },
        )
        self._session.execute(stmt)
