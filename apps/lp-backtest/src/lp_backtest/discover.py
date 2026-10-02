"""候选池发现 CLI：扫描 PancakeSwap V3 Factory 的 `PoolCreated` 事件，落库到 `pool_candidates`。

增量扫描：水位线记录在 `chain_cursors`，只扫描上次结束之后到"已终结区块"（当前最新区块减去
`CONFIRMATION_BLOCKS` 安全边际）之间的区间，避免链重组导致重复/遗漏。首次运行没有水位线时，
按协议插件声明的"上线时间锚点"二分定位起始区块（只需要区块时间戳，不依赖 archive node）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import click
from alpha_chains.bsc import build_bsc_adapter
from alpha_protocols.identification.factory_discovery import iter_discover_pools
from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin
from alpha_storage.db import session_scope
from alpha_storage.repositories.chain_cursor import ChainCursorRepository
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# BSC 出块极快（0.45s），但仍保留安全边际，避免把尚未终结的尾部区块当作"已扫描"记入水位线。
CONFIRMATION_BLOCKS = 20


@click.command()
@click.option("--step", default=500_000, show_default=True, help="单批扫描的区块步长")
@click.option(
    "--to-block",
    type=int,
    default=None,
    help="结束区块（含），默认取最新区块减安全边际；调试时可传较小值限定范围",
)
@click.option(
    "--from-block",
    type=int,
    default=None,
    help="强制指定起始区块，忽略已有水位线；仅用于调试或重新回溯",
)
@click.option(
    "--since-days-ago",
    type=int,
    default=None,
    help=(
        "只扫描大约 N 天前至今的历史（会二分定位对应区块号，忽略已有水位线和协议上线锚点）。"
        "例如 --since-days-ago 365 表示放弃一年前之前的候选池，只找近一年新开的池子。"
        "和 --from-block 二选一，不能同时传。"
    ),
)
def main(
    step: int, to_block: int | None, from_block: int | None, since_days_ago: int | None
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    if from_block is not None and since_days_ago is not None:
        raise click.UsageError("--from-block 和 --since-days-ago 不能同时传")

    adapter = build_bsc_adapter()
    plugin = PancakeswapV3Plugin()

    with session_scope() as session:
        cursor_repo = ChainCursorRepository(session)
        watermark = cursor_repo.get_last_scanned_block(plugin.chain, plugin.dex_id)

    if from_block is not None:
        start_block = from_block
    elif since_days_ago is not None:
        target = datetime.now(UTC) - timedelta(days=since_days_ago)
        logger.info("按 --since-days-ago=%d 二分定位起始区块，目标时间 %s", since_days_ago, target)
        start_block = adapter.find_block_by_timestamp(target)
        logger.info("起始区块: %d（放弃此前的候选池，只发现这之后新开的池子）", start_block)
    elif watermark is not None:
        start_block = watermark + 1
    else:
        hint = plugin.launch_date_hint_utc()
        logger.info("无历史水位线，按协议上线时间锚点 %s 二分定位起始区块", hint)
        start_block = adapter.find_block_by_timestamp(hint)
        logger.info("起始区块: %d", start_block)

    end_block = to_block if to_block is not None else adapter.get_latest_block() - CONFIRMATION_BLOCKS

    if start_block > end_block:
        logger.info("没有新区块需要扫描（起点 %d > 终点 %d）", start_block, end_block)
        return

    logger.info("开始扫描 [%d, %d]，步长 %d", start_block, end_block, step)
    total = 0
    for chunk_end, candidates in iter_discover_pools(
        adapter, plugin, from_block=start_block, to_block=end_block, step=step
    ):
        with session_scope() as session:
            PoolCandidateRepository(session).upsert_many(candidates)
            ChainCursorRepository(session).advance(plugin.chain, plugin.dex_id, chunk_end)
        total += len(candidates)
        logger.info("已扫描到区块 %d，累计发现 %d 个候选池", chunk_end, total)

    logger.info("扫描完成，本次共发现 %d 个候选池", total)


if __name__ == "__main__":
    main()
