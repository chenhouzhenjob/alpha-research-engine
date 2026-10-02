"""历史数据接入 CLI：先对 `discovered` 候选池跑准入判定，再对 `qualified` 候选池拉取历史快照。

依赖 `lp-backtest-discover` 先把 Factory 全量池子扫进 `pool_candidates`。
"""

from __future__ import annotations

import logging

import click
from alpha_chains.bsc import build_bsc_adapter
from alpha_core.types import Chain, PoolCandidateStatus
from alpha_datasources.geckoterminal import GeckoTerminalClient
from alpha_metrics.snapshots import backfill_snapshots, build_subgraph_client_from_env
from alpha_storage.db import session_scope
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository
from dotenv import load_dotenv

from .config import DEFAULT_BACKFILL_DAYS
from .qualify import qualify_candidates

logger = logging.getLogger(__name__)


@click.command()
@click.option("--backfill-days", default=DEFAULT_BACKFILL_DAYS, show_default=True)
@click.option("--limit", type=int, default=None, help="最多处理多少个 qualified 候选池，调试用")
@click.option("--skip-qualify", is_flag=True, default=False, help="跳过准入判定，只对已 qualified 的池子拉快照")
@click.option(
    "--pool-address",
    "pool_addresses_filter",
    multiple=True,
    help="只回填指定池子（可重复传入）；不传则回填全部 qualified 候选池。"
    "跟 --limit 同时传时，先按这个过滤，--limit 不生效（两者语义冲突，这个更精确）",
)
def main(
    backfill_days: int, limit: int | None, skip_qualify: bool, pool_addresses_filter: tuple[str, ...]
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    chain = Chain.BSC
    gecko = GeckoTerminalClient(network=chain.value)

    if not skip_qualify:
        adapter = build_bsc_adapter()
        with session_scope() as session:
            qualified, rejected = qualify_candidates(session, adapter, gecko, chain)
        logger.info("准入判定：新增 qualified=%d rejected=%d", qualified, rejected)

    with session_scope() as session:
        qualified_rows = PoolCandidateRepository(session).list_by_status(
            chain, PoolCandidateStatus.QUALIFIED
        )
        pool_addresses = [row.pool_address for row in qualified_rows]

    if pool_addresses_filter:
        requested = {addr.lower() for addr in pool_addresses_filter}
        pool_addresses = [addr for addr in pool_addresses if addr in requested]
        missing = requested - set(pool_addresses)
        if missing:
            logger.warning("以下地址不在 qualified 候选池里，跳过: %s", sorted(missing))
    elif limit is not None:
        pool_addresses = pool_addresses[:limit]

    logger.info("开始为 %d 个 qualified 候选池回填近 %d 天历史快照", len(pool_addresses), backfill_days)

    subgraph = build_subgraph_client_from_env()
    with session_scope() as session:
        total_rows = backfill_snapshots(
            session, gecko, chain, pool_addresses, backfill_days=backfill_days, subgraph=subgraph
        )

    logger.info("历史快照接入完成，共写入 %d 行", total_rows)


if __name__ == "__main__":
    main()
