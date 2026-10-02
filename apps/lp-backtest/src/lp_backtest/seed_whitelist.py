"""白名单直连发现 CLI：不扫描 Factory 历史事件，直接按白名单 token 两两配对 + 标准费率档位
调用 `Factory.getPool` 查询是否已存在池子。

用途边界（务必先读）：只能发现"两侧都在白名单里"的池子（如 CAKE/WBNB、USDT/WBNB），
覆盖不到"白名单 token 配未知长尾币"的场景（如 SHIB/WBNB）——那种场景本质上需要知道
长尾币地址，只能靠扫描 `PoolCreated` 事件（`lp-backtest-discover`）来发现，没有捷径。

适合的场景：还不想跑一次全量历史区块扫描，先用白名单内部的"蓝筹"配对把 ingest 全链路
（准入判定 + 历史快照回填）跑起来验证一遍。跑完 `lp-backtest-discover` 之后，
这条命令产出的候选池会和扫描发现的候选池共存于同一张 `pool_candidates` 表，不冲突。
"""

from __future__ import annotations

import logging
from itertools import combinations

import click
from alpha_chains.bsc import build_bsc_adapter
from alpha_protocols.plugins.pancakeswap_v3 import FEE_TIER_TICK_SPACING, PancakeswapV3Plugin
from alpha_storage.db import session_scope
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository
from dotenv import load_dotenv

from .config import WHITELIST_TOKENS_BSC

logger = logging.getLogger(__name__)


@click.command()
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    adapter = build_bsc_adapter()
    plugin = PancakeswapV3Plugin()

    tokens = sorted(WHITELIST_TOKENS_BSC)
    pairs = list(combinations(tokens, 2))
    logger.info(
        "开始按白名单两两配对直连查询：%d 个 token，%d 个组合 × %d 档费率 = 最多 %d 次 eth_call",
        len(tokens),
        len(pairs),
        len(FEE_TIER_TICK_SPACING),
        len(pairs) * len(FEE_TIER_TICK_SPACING),
    )

    candidates = []
    for token_a, token_b in pairs:
        for fee in FEE_TIER_TICK_SPACING:
            candidate = plugin.find_pool_by_tokens(adapter, token_a, token_b, fee)
            if candidate is not None:
                candidates.append(candidate)

    with session_scope() as session:
        PoolCandidateRepository(session).upsert_many(candidates)

    logger.info("直连查询完成，发现 %d 个已存在的白名单内部配对池子", len(candidates))
    logger.info("下一步跑 lp-backtest-ingest 做准入判定 + 历史快照回填")


if __name__ == "__main__":
    main()
