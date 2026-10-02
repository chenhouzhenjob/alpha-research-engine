"""BSC（BNB Chain）链适配器。

HTTP 适配器由通用的 `factory.build_evm_adapter` 按 `CHAIN_SPECS[Chain.BSC]` 构造。RPC 端点复用
alpha-lp 同款环境变量 `BNB_RPC_URLS`（逗号分隔，第一个为主，其余为兜底），避免运维为同一条链的
RPC 访问维护两套密钥。BSC 单次 eth_getLogs 的默认跨度 45000 也登记在链规格里：alpha-lp 生产
RPC 实测上限是 50000 个区块（报错 "exceed maximum block range: 50000"），换了 RPC 提供商导致
不适用时用 `BNB_LOG_CHUNK_SIZE` 覆盖。
"""

from __future__ import annotations

import os

from alpha_core.metering import CallMeter
from alpha_core.ports import BlockTimeStore
from alpha_core.types import Chain

from .evm_common import EvmChainAdapter
from .evm_websocket import EvmWebSocketSubscriber
from .factory import build_evm_adapter

WSS_URL_ENV = "BNB_WSS_URL"


def build_bsc_adapter(
    *, meter: CallMeter | None = None, block_time_store: BlockTimeStore | None = None
) -> EvmChainAdapter:
    """从环境变量构造 BSC 适配器，等同于 `build_evm_adapter(Chain.BSC, ...)`。

    @param meter 外部调用计量器；不传则不记账（lp-backtest/live-signal 现有调用方不传，行为不变）
    @param block_time_store 区块时间持久化缓存；不传则只缓存在进程内存
    @raises ValueError `BNB_RPC_URLS` 未配置
    """
    return build_evm_adapter(Chain.BSC, meter=meter, block_time_store=block_time_store)


def build_bsc_wss_subscriber() -> EvmWebSocketSubscriber:
    """从环境变量构造 BSC 的 WebSocket 订阅器。

    阶段 0 只支持单个端点（`BNB_WSS_URL`），不像 `build_bsc_adapter` 那样支持逗号分隔的
    多端点故障转移——`EvmWebSocketSubscriber` 本身的多端点能力是已知的范围缩减
    （见 evm_websocket 模块文档），以后要支持时在这里改，不需要动调用方。

    @raises ValueError `BNB_WSS_URL` 未配置
    """
    wss_url = os.environ.get(WSS_URL_ENV, "").strip()
    if not wss_url:
        raise ValueError(f"环境变量 {WSS_URL_ENV} 未配置，无法构造 BSC WebSocket 订阅器")
    return EvmWebSocketSubscriber(wss_url)
