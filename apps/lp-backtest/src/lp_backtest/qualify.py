"""候选池准入判定：token 白名单 + 池龄 + TVL/24h volume 门槛（pool-discovery-metrics-v1.md 第 0 节）。

未达门槛的池子标记为 rejected，不产生分数、不纳入历史快照采集范围，避免噪音池占用存储和算力。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from alpha_chains.base import ChainAdapter
from alpha_core.types import Chain, PoolCandidateStatus
from alpha_datasources.base import PoolMarketSnapshot
from alpha_datasources.geckoterminal import GeckoTerminalClient
from alpha_storage.models import PoolCandidateRow
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository

from .config import MIN_POOL_AGE_DAYS, MIN_TVL_USD, MIN_VOLUME_24H_USD, is_whitelisted_pool

logger = logging.getLogger(__name__)


def _resolve_created_at(
    adapter: ChainAdapter, candidate: PoolCandidateRow, snapshot: PoolMarketSnapshot | None
) -> datetime | None:
    """确定候选池的创建时间，两条路径二选一：

    1. `created_at_block > 0`（Factory 事件扫描发现）：查链上区块时间戳，权威、精确。
    2. `created_at_block == 0`（`find_pool_by_tokens` 直连查询发现，没有区块号）：
       退化用 GeckoTerminal 报告的 `pool_created_at` 兜底——这个值不是链上权威值，
       但对"判断池龄是否 >= 7 天"这个粗粒度门槛够用。两种情况都拿不到时返回 None，
       调用方应当把该候选池视为"暂时无法判定"，不能默认放行或默认拒绝。
    """
    if candidate.created_at is not None:
        return candidate.created_at
    if candidate.created_at_block > 0:
        ts = adapter.get_block_timestamp(candidate.created_at_block)
        candidate.created_at = ts
        return ts
    if snapshot is not None and snapshot.created_at is not None:
        candidate.created_at = snapshot.created_at
        return snapshot.created_at
    return None


def qualify_candidates(
    session,
    adapter: ChainAdapter,
    gecko: GeckoTerminalClient,
    chain: Chain,
) -> tuple[int, int]:
    """对所有 `discovered` 状态的候选池跑一遍准入判定，原地更新 status。

    @returns (转为 qualified 的数量, 转为 rejected 的数量)
    """
    repo = PoolCandidateRepository(session)
    discovered = repo.list_by_status(chain, PoolCandidateStatus.DISCOVERED)
    if not discovered:
        return 0, 0

    # 先过 token 白名单（纯本地判断，不消耗任何外部请求），未命中的直接拒绝。
    whitelisted = []
    rejected = 0
    for row in discovered:
        if is_whitelisted_pool(row.token0_address, row.token1_address):
            whitelisted.append(row)
        else:
            row.status = PoolCandidateStatus.REJECTED.value
            row.updated_at = datetime.now(UTC)
            rejected += 1

    # 一次性批量取快照：TVL/volume 门槛要用，池龄兜底（见 _resolve_created_at）也要用。
    now = datetime.now(UTC)
    snapshots = gecko.get_pool_snapshots([row.pool_address for row in whitelisted])

    eligible_by_age = []
    for row in whitelisted:
        created_at = _resolve_created_at(adapter, row, snapshots.get(row.pool_address))
        if created_at is None or (now - created_at).days < MIN_POOL_AGE_DAYS:
            row.status = PoolCandidateStatus.REJECTED.value
            row.updated_at = now
            rejected += 1
        else:
            eligible_by_age.append(row)

    # TVL / 24h volume 门槛：复用上面已经批量取好的快照，不重复请求。
    qualified = 0
    for row in eligible_by_age:
        snapshot = snapshots.get(row.pool_address)
        passes = (
            snapshot is not None
            and (snapshot.tvl_usd or 0) >= MIN_TVL_USD
            and (snapshot.volume_24h_usd or 0) >= MIN_VOLUME_24H_USD
        )
        row.status = (
            PoolCandidateStatus.QUALIFIED.value if passes else PoolCandidateStatus.REJECTED.value
        )
        row.updated_at = now
        if passes:
            qualified += 1
        else:
            rejected += 1

    logger.info("准入判定完成：qualified=%d rejected=%d（共 %d 个待判定）", qualified, rejected, len(discovered))
    return qualified, rejected
