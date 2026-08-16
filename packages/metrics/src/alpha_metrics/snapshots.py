"""历史快照采集：把 GeckoTerminal 的 OHLCV 收盘价 + 历史 TVL/24h volume 写入
`pool_metrics_history`。

从 `apps/lp-backtest` 搬到这里——`lp-backtest-ingest`（历史候选池批量回填）和
`apps/live-signal` 的常驻定时任务（阶段 0 试点池子的每日回填）都要用同一份逻辑，
`apps` 之间不互相依赖，所以抽到这个共享包。

**历史 TVL/24h volume 现在有两个来源**：GeckoTerminal 只暴露"当前值"（见其模块文档），
`subgraph`（`alpha_datasources.pancakeswap_subgraph`）是真正的按天历史索引——传了这个参数
就会用它把历史行的 `tvl_usd`/`volume_24h_usd` 填上，不传（`subgraph=None`）就退化回
"只有今天这一行是真实值，历史行是 NULL"的旧行为，不是硬依赖（没有 `THEGRAPH_API_KEY` 时
调用方可以选择不传）。
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime

from alpha_core.models import PoolMetricsSnapshot
from alpha_core.types import Chain
from alpha_datasources.geckoterminal import GeckoTerminalClient
from alpha_datasources.pancakeswap_subgraph import PancakeswapSubgraphClient, SubgraphDailySnapshot
from alpha_storage.repositories.pool_metrics import PoolMetricsRepository

logger = logging.getLogger(__name__)

THEGRAPH_API_KEY_ENV = "THEGRAPH_API_KEY"


def build_subgraph_client_from_env() -> PancakeswapSubgraphClient | None:
    """两个调用方（`lp-backtest-ingest`、`live-signal` 的每日回填任务）都要"有 key 就用，
    没有就退化"这段逻辑，抽出来避免各写一份。
    """
    api_key = os.environ.get(THEGRAPH_API_KEY_ENV)
    if not api_key:
        logger.info("%s 未配置，历史 TVL/volume 退化为只有当天是真实值", THEGRAPH_API_KEY_ENV)
        return None
    return PancakeswapSubgraphClient(api_key)


def backfill_snapshots(
    session,
    gecko: GeckoTerminalClient,
    chain: Chain,
    pool_addresses: list[str],
    *,
    backfill_days: int,
    subgraph: PancakeswapSubgraphClient | None = None,
) -> int:
    """对每个池子拉取近 `backfill_days` 天的日线收盘价 + 历史/当前 TVL/24h volume，
    写入历史快照表。

    @returns 写入的快照行数
    """
    if not pool_addresses:
        return 0

    current_snapshots = gecko.get_pool_snapshots(pool_addresses)
    total_rows = 0

    for pool_address in pool_addresses:
        ohlcv = gecko.get_daily_ohlcv(pool_address, days=backfill_days)
        fetched_at = datetime.now(UTC)

        subgraph_by_date: dict[date, SubgraphDailySnapshot] = {}
        if subgraph is not None:
            subgraph_by_date = {
                s.day: s for s in subgraph.get_daily_tvl_volume(pool_address, days=backfill_days)
            }

        rows_by_date: dict[date, PoolMetricsSnapshot] = {}
        for point in ohlcv:
            sg = subgraph_by_date.get(point.day)
            rows_by_date[point.day] = PoolMetricsSnapshot(
                chain=chain,
                pool_address=pool_address,
                snapshot_date=point.day,
                tvl_usd=sg.tvl_usd if sg else None,
                volume_24h_usd=sg.volume_24h_usd if sg else None,
                close_price=point.close,
                data_source="gt+subgraph" if sg else "geckoterminal",
                fetched_at=fetched_at,
            )
        # subgraph 覆盖的天数可能比 GeckoTerminal OHLCV 多（两边索引进度不完全一致），
        # 这些日期也要落一行，只是没有 close_price。
        for day, sg in subgraph_by_date.items():
            if day not in rows_by_date:
                rows_by_date[day] = PoolMetricsSnapshot(
                    chain=chain,
                    pool_address=pool_address,
                    snapshot_date=day,
                    tvl_usd=sg.tvl_usd,
                    volume_24h_usd=sg.volume_24h_usd,
                    close_price=None,
                    data_source="subgraph",
                    fetched_at=fetched_at,
                )

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
