"""实时 Swap 事件订阅：对配置的池子（阶段 0 是 `LIVE_SIGNAL_POOL_ADDRESSES`，都是
PancakeSwap V3 BSC）开 WebSocket 订阅，逐笔解码落库到 `swap_events`。

从 `apps/lp-backtest` 的 `subscribe_swaps.py` 搬过来——这个逐笔订阅本来就是为了给
`live-signal` 提供数据（K 线现在走 GeckoTerminal 轮询，不再依赖这批数据；这批数据留着是
给以后的风控信号，如大户集中度、刷量模式识别用），放在 `lp_backtest`（"历史回测校准"
定位）下不是它该待的地方，属于架构纠偏，不是随便挪。

断线重连：`EvmWebSocketSubscriber.subscribe_logs` 断开时会抛出异常结束，外层循环捕获后立即
重连；重连前先用现有 `ChainAdapter.get_logs`（HTTP）从 `swap_events` 里已落库的最大
`block_number`（`SwapEventRepository.get_max_block_number`）+1 回填到当前最新区块，
避免断线期间漏掉的 Swap 事件永久丢失（水位线直接用 `swap_events` 自身，不复用
`chain_cursors`，见 `SwapEventRepository.get_max_block_number` 的注释）。

阶段 0 范围：只支持 PancakeSwap V3（`PancakeswapV3Plugin`），多协议按 `dex_id` 分发是以后
接入新 DEX 时才需要的东西，现在只有这一种候选池，不提前做。

已知的效率问题（阶段 0 只有 2 个池子，接受）：每个池子各开一条独立的 WebSocket 连接，
而 `EvmWebSocketSubscriber` 的 `address` 客户端过滤（见其模块文档）意味着每条连接实际都在
接收全链的 Swap 事件、自己过滤掉不相关的——池子一多，会有 N 条连接重复接收同一份全量数据。
以后池子数量上来了，应该改成一条连接 + 按 topics 订阅、进程内按 address 分发给各池子的处理逻辑，
不是本次改动的范围。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from alpha_chains.base import ChainAdapter
from alpha_chains.bsc import build_bsc_adapter, build_bsc_wss_subscriber
from alpha_core.types import Chain
from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin
from alpha_storage.db import session_scope
from alpha_storage.repositories.swap_events import SwapEventRepository

logger = logging.getLogger(__name__)

_RECONNECT_DELAY_SECONDS = 2.0


async def _backfill(
    pool_address: str, chain: Chain, plugin: PancakeswapV3Plugin, topic0: str, adapter: ChainAdapter
) -> None:
    """从 `swap_events` 已落库的最大区块回填到当前最新区块。首次启动（还没有任何事件）
    时不回填——阶段 0 只关心"从现在开始"的实时数据，不是历史回溯（历史数据是
    lp-backtest-ingest 的职责）。

    这条路径走 HTTP `get_logs`，拿不到 WebSocket 订阅 payload 里免费带的 `blockTimestamp`
    （见 `evm_websocket` 模块文档），所以要现场调 `get_block_timestamp` 补齐 `block_time`——
    按去重后的区块号查（同一区块内的多笔 swap 共用一个时间戳），减少 RPC 调用次数；
    这条路径只在断线重连时触发，频率远低于持续在线的 WS 主路径，接受这个成本。
    """
    with session_scope() as session:
        max_block = SwapEventRepository(session).get_max_block_number(chain, pool_address)
    if max_block is None:
        return

    latest_block = await asyncio.to_thread(adapter.get_latest_block)
    if max_block >= latest_block:
        return

    logs = await asyncio.to_thread(
        adapter.get_logs,
        address=pool_address,
        topics=[topic0],
        from_block=max_block + 1,
        to_block=latest_block,
    )
    unique_blocks = sorted({log.block_number for log in logs})
    block_times = {b: await asyncio.to_thread(adapter.get_block_timestamp, b) for b in unique_blocks}
    events = [
        plugin.decode_swap_event(log, fetched_at=datetime.now(UTC), block_time=block_times[log.block_number])
        for log in logs
    ]
    if events:
        with session_scope() as session:
            SwapEventRepository(session).upsert_many(events)
    logger.info(
        "回填 %s: 区块 [%d, %d] 共 %d 条 Swap 事件", pool_address, max_block + 1, latest_block, len(events)
    )


async def _subscribe_one_pool(pool_address: str) -> None:
    """持续订阅 + 断线重连，没有内部超时——这个协程被 `run_forever` 用 `asyncio.gather`
    跟其他池子一起启动，靠外层（`main.py` 的 lifespan）在应用关闭时 `task.cancel()` 停止，
    不靠自己判断"该退出了"。
    """
    chain = Chain.BSC
    plugin = PancakeswapV3Plugin()
    topic0 = plugin.swap_topic0()
    # 只建一个 adapter 实例，跨重连循环复用——EvmChainAdapter 的 get_block_timestamp 有
    # 进程内缓存，复用同一个实例才能吃到缓存（新建实例缓存清零），断线回填路径尤其受益。
    adapter = build_bsc_adapter()

    while True:
        await _backfill(pool_address, chain, plugin, topic0, adapter)
        subscriber = build_bsc_wss_subscriber()
        try:
            async for log in subscriber.subscribe_logs(address=pool_address, topics=[topic0]):
                block_time = log.block_time
                if block_time is None:
                    # 防御性兜底：正常情况下 WebSocket payload 应该带 blockTimestamp
                    # （见 evm_websocket 模块文档），真的没有时退化成现查一次，不能让
                    # block_time 缺失导致这条事件没法落库。
                    block_time = await asyncio.to_thread(adapter.get_block_timestamp, log.block_number)
                event = plugin.decode_swap_event(log, fetched_at=datetime.now(UTC), block_time=block_time)
                with session_scope() as session:
                    SwapEventRepository(session).upsert_many([event])
                logger.info(
                    "%s 新 Swap: block=%d tick=%d amount0=%s amount1=%s",
                    pool_address,
                    event.block_number,
                    event.tick_after,
                    event.amount0,
                    event.amount1,
                )
        except Exception:  # noqa: BLE001 - 断线原因多样（超时/远端关闭/网络抖动），统一走重连
            # asyncio.CancelledError 继承自 BaseException 不是 Exception，应用关闭时
            # task.cancel() 触发的取消不会被这里捕获，会正常向上传播，不会被误当成断线重连。
            logger.warning("%s WebSocket 订阅断开，%.0f 秒后重连并回填缺口", pool_address, _RECONNECT_DELAY_SECONDS)
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)


async def run_forever(pool_addresses: list[str]) -> None:
    """常驻订阅入口，`main.py` 的 lifespan 用 `asyncio.create_task` 启动，应用关闭时取消。"""
    logger.info("开始订阅 %d 个池子的 Swap 事件: %s", len(pool_addresses), pool_addresses)
    await asyncio.gather(*(_subscribe_one_pool(addr) for addr in pool_addresses))
