"""第一层协议识别：工厂自动发现（钱包链上行为分析设计方案 3.2 第一层）。

只需要注册一个 Factory 地址，扫描它的历史 `PoolCreated` 事件即可自动生成全量池子表，
以后新上线的池子也会随扫描自动收录，不需要任何人工干预。
"""

from __future__ import annotations

import logging

from alpha_chains.base import ChainAdapter
from alpha_core.models import PoolCandidate

from ..base import FactoryDiscoveryPlugin

logger = logging.getLogger(__name__)

# 单次扫描的默认区块步长；与 chains 层的 eth_getLogs 分段是两回事——
# 这里的步长决定一次 discover_pools 调用扫多大区间，chains 层再按 RPC 限制进一步切分。
DEFAULT_SCAN_STEP_BLOCKS = 500_000


def discover_pools(
    adapter: ChainAdapter,
    plugin: FactoryDiscoveryPlugin,
    *,
    from_block: int,
    to_block: int,
) -> list[PoolCandidate]:
    """扫描 `[from_block, to_block]` 区间内 Factory 发出的全部 `PoolCreated` 事件。

    @param adapter 目标链的适配器
    @param plugin 声明了 Factory 地址与解码逻辑的协议插件
    @param from_block 起始区块（含），调用方通常传入链级扫描水位线
    @param to_block 结束区块（含），调用方通常传入"已终结区块"而非最新区块，避免链重组导致重复
    @returns 本次扫描新发现的候选池列表，按区块号升序
    """
    topic0 = plugin.pool_created_topic0()
    logs = adapter.get_logs(
        address=plugin.factory_address,
        topics=[topic0],
        from_block=from_block,
        to_block=to_block,
    )
    candidates = [plugin.decode_pool_created(log) for log in logs]
    logger.info("工厂发现：%s [%d, %d] 扫到 %d 个新池子", plugin.dex_id, from_block, to_block, len(candidates))
    return candidates


def iter_discover_pools(
    adapter: ChainAdapter,
    plugin: FactoryDiscoveryPlugin,
    *,
    from_block: int,
    to_block: int,
    step: int = DEFAULT_SCAN_STEP_BLOCKS,
):
    """按 `step` 分批扫描并逐批 yield，避免一次性把整段历史的日志都留在内存里。

    调用方应当在每批处理完成后落库并推进水位线，即使中途失败也只需要从最后一批成功的位置续扫。
    """
    cursor = from_block
    while cursor <= to_block:
        chunk_end = min(cursor + step - 1, to_block)
        yield chunk_end, discover_pools(adapter, plugin, from_block=cursor, to_block=chunk_end)
        cursor = chunk_end + 1
