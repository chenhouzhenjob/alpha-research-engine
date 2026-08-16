"""BSC（BNB Chain）链适配器。

RPC 端点复用 alpha-lp 同款环境变量 `BNB_RPC_URLS`（逗号分隔，第一个为主，其余为兜底），
避免运维为同一条链的 RPC 访问维护两套密钥。
"""

from __future__ import annotations

import os

from alpha_core.types import Chain

from .evm_common import EvmChainAdapter
from .evm_websocket import EvmWebSocketSubscriber

BSC_CHAIN_ID = 56
RPC_URLS_ENV = "BNB_RPC_URLS"
LOG_CHUNK_SIZE_ENV = "BNB_LOG_CHUNK_SIZE"
WSS_URL_ENV = "BNB_WSS_URL"

# 实测 alpha-lp 生产 BNB_RPC_URLS 配置的 RPC 单次 eth_getLogs 最多接受 50000 个区块
# （报错信息为 "exceed maximum block range: 50000"）。取略低于上限的保守值作为 BSC 专用默认值，
# 比 EvmChainAdapter 通用默认的 2000 大 20 倍，能显著减少全量历史回填所需的调用次数。
# 如果换了 RPC 提供商导致这个值不适用，用 BNB_LOG_CHUNK_SIZE 环境变量覆盖，不用改代码。
DEFAULT_BSC_LOG_CHUNK_SIZE = 45_000


def build_bsc_adapter() -> EvmChainAdapter:
    """从环境变量构造 BSC 适配器。

    @raises ValueError `BNB_RPC_URLS` 未配置
    """
    raw = os.environ.get(RPC_URLS_ENV, "")
    rpc_urls = [url.strip() for url in raw.split(",") if url.strip()]
    if not rpc_urls:
        raise ValueError(f"环境变量 {RPC_URLS_ENV} 未配置，无法构造 BSC 链适配器")

    chunk_size_raw = os.environ.get(LOG_CHUNK_SIZE_ENV, "")
    if chunk_size_raw.strip():
        log_chunk_size = int(chunk_size_raw)
    else:
        log_chunk_size = DEFAULT_BSC_LOG_CHUNK_SIZE

    return EvmChainAdapter(
        chain=Chain.BSC, rpc_urls=rpc_urls, is_poa=True, log_chunk_size=log_chunk_size
    )


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
