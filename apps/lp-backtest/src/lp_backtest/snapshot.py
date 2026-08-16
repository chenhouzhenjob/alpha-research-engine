"""历史快照采集：把 GeckoTerminal 的 OHLCV 收盘价 + 当前 TVL/24h volume 写入 `pool_metrics_history`。

已知局限（见 packages/datasources 的 geckoterminal.py 模块说明，同步在此提醒调用方）：
TVL/24h volume 只有"今天"这一行是真实值，历史行的 tvl_usd/volume_24h_usd 会是 NULL——
这两个指标的历史序列需要本系统逐日运行 ingest 才能积累出来，无法一次性回填。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from alpha_core.models import PoolMetricsSnapshot
from alpha_core.types import Chain
from alpha_datasources.geckoterminal import GeckoTerminalClient
from alpha_storage.repositories.pool_metrics import PoolMetricsRepository

logger = logging.getLogger(__name__)


def backfill_snapshots(
    session,
    gecko: GeckoTerminalClient,
    chain: Chain,
    pool_addresses: list[str],
    *,
    backfill_days: int,
) -> int:
    """对每个池子拉取近 `backfill_days` 天的日线收盘价 + 当前 TVL/24h volume，写入历史快照表。

    @returns 写入的快照行数
    """
    if not pool_addresses:
        return 0

    current_snapshots = gecko.get_pool_snapshots(pool_addresses)
    total_rows = 0

    for pool_address in pool_addresses:
        ohlcv = gecko.get_daily_ohlcv(pool_address, days=backfill_days)
        fetched_at = datetime.now(UTC)
        rows_by_date = {
            point.day: PoolMetricsSnapshot(
                chain=chain,
                pool_address=pool_address,
                snapshot_date=point.day,
                tvl_usd=None,
                volume_24h_usd=None,
                close_price=point.close,
                data_source="geckoterminal",
                fetched_at=fetched_at,
            )
            for point in ohlcv
        }

        current = current_snapshots.get(pool_address)
        if current is not None:
            today = current.fetched_at.date()
            existing = rows_by_date.get(today)
            rows_by_date[today] = PoolMetricsSnapshot(
                chain=chain,
                pool_address=pool_address,
                snapshot_date=today,
                tvl_usd=current.tvl_usd,
                volume_24h_usd=current.volume_24h_usd,
                close_price=current.close_price if current.close_price is not None else (
                    existing.close_price if existing else None
                ),
                data_source="geckoterminal",
                fetched_at=current.fetched_at,
            )

        if not rows_by_date:
            logger.warning("池子无任何可用快照数据: %s", pool_address)
            continue

        PoolMetricsRepository(session).upsert_many(list(rows_by_date.values()))
        total_rows += len(rows_by_date)

    return total_rows
