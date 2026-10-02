"""每日历史快照回填：把 GeckoTerminal 的收盘价历史 + 历史 TVL/24h volume 写入
`pool_metrics_history`——供 `alpha_metrics` 的 VTRatio/σ_price/CompositeScore 等特征计算用。

只对 `LIVE_SIGNAL_POOL_ADDRESSES` 配置的池子跑，不做 `qualify_candidates` 那套全量准入判定——
这几个池子是 `lp-backtest-register-pool` 手动核实过注册进来的，不需要走自动发现+准入判定流程
（跟 `lp-backtest-ingest --skip-qualify` 的语义一致）。`lp-backtest-ingest`（不带
`--skip-qualify`）仍然是 1a-1f 阶段那批历史候选池的入口，两者共用
`alpha_metrics.snapshots.backfill_snapshots`，不重复实现。

历史 TVL/24h volume 现在走 PancakeSwap 官方 Subgraph（`THEGRAPH_API_KEY` 配置了就会用，
没配置就退化回"只有今天是真实值"的旧行为，见 `alpha_metrics.snapshots` 的模块文档）。
"""

from __future__ import annotations

import asyncio
import logging

from alpha_core.types import Chain
from alpha_datasources.geckoterminal import GeckoTerminalClient
from alpha_metrics.snapshots import backfill_snapshots, build_subgraph_client_from_env
from alpha_storage.db import session_scope

logger = logging.getLogger(__name__)

DEFAULT_BACKFILL_DAYS = 30
INGEST_INTERVAL_SECONDS = 24 * 60 * 60  # 一天一次——TVL/24h volume 只有"今天"是真实值，
# 跑更勤也不会有更多历史信息，见 alpha_metrics.snapshots 的模块文档


def _run_once_sync(pool_addresses: list[str]) -> int:
    gecko = GeckoTerminalClient(network="bsc")
    subgraph = build_subgraph_client_from_env()
    with session_scope() as session:
        return backfill_snapshots(
            session, gecko, Chain.BSC, pool_addresses, backfill_days=DEFAULT_BACKFILL_DAYS, subgraph=subgraph
        )


async def run_forever(pool_addresses: list[str], *, interval_seconds: float = INGEST_INTERVAL_SECONDS) -> None:
    """常驻回填入口，`main.py` 的 lifespan 用 `asyncio.create_task` 启动，应用关闭时取消。"""
    logger.info("开始每日历史快照回填（每 %.0f 秒一次）: %s", interval_seconds, pool_addresses)
    while True:
        try:
            total_rows = await asyncio.to_thread(_run_once_sync, pool_addresses)
            logger.info("历史快照回填完成，共写入 %d 行", total_rows)
        except Exception:  # noqa: BLE001 - 这一轮失败不能让整个回填循环退出，等下一轮重试
            logger.exception("历史快照回填失败，等下一轮重试")
        await asyncio.sleep(interval_seconds)
