"""按链构造 EVM 链适配器。

链的规格（chainId、环境变量前缀、是否 PoA、默认 getLogs 跨度）登记在 `alpha_core.types.CHAIN_SPECS`，
新增一条 EVM 链只需要登记规格和配置环境变量，不用再写一个 `build_<chain>_adapter`。
"""

from __future__ import annotations

import os

from alpha_core.metering import CallMeter
from alpha_core.ports import BlockTimeStore
from alpha_core.types import CHAIN_SPECS, Chain

from .evm_common import DEFAULT_BATCH_SIZE, DEFAULT_LOG_CHUNK_SIZE, EvmChainAdapter
from .rate_limit import CuTokenBucket


def rpc_urls_env(chain: Chain) -> str:
    """该链 RPC 端点列表的环境变量名，例如 BSC 为 `BNB_RPC_URLS`。"""
    return f"{CHAIN_SPECS[chain].env_prefix}_RPC_URLS"


def _optional_number(env: str) -> float | None:
    raw = os.environ.get(env, "").strip()
    return float(raw) if raw else None


def build_evm_adapter(
    chain: Chain, *, meter: CallMeter | None = None, block_time_store: BlockTimeStore | None = None
) -> EvmChainAdapter:
    """从环境变量构造指定链的适配器。

    读取的环境变量（`<前缀>` 见 `ChainSpec.env_prefix`）：
    - `<前缀>_RPC_URLS`：逗号分隔，第一个为主，其余为兜底（必填）；
    - `<前缀>_LOG_CHUNK_SIZE`：单次 eth_getLogs 的区块跨度，不配置用链规格的默认值；
    - `<前缀>_RPC_MAX_CUPS`：每秒最多消耗的 CU，不配置则不限速；
    - `<前缀>_RPC_BATCH_SIZE`：一次 JSON-RPC 批量请求的最大调用数，不配置默认 50。

    @param meter 外部调用计量器；不传则不记账
    @param block_time_store 区块时间持久化缓存；不传则只缓存在进程内存
    @raises KeyError 链没有登记规格
    @raises ValueError RPC 端点未配置
    """
    spec = CHAIN_SPECS[chain]
    prefix = spec.env_prefix
    env = rpc_urls_env(chain)
    rpc_urls = [url.strip() for url in os.environ.get(env, "").split(",") if url.strip()]
    if not rpc_urls:
        raise ValueError(f"环境变量 {env} 未配置，无法构造 {chain} 链适配器")

    chunk = _optional_number(f"{prefix}_LOG_CHUNK_SIZE")
    batch_size = _optional_number(f"{prefix}_RPC_BATCH_SIZE")
    return EvmChainAdapter(
        chain=chain,
        rpc_urls=rpc_urls,
        is_poa=spec.is_poa,
        log_chunk_size=int(chunk) if chunk else (spec.default_log_chunk_size or DEFAULT_LOG_CHUNK_SIZE),
        meter=meter,
        rate_limiter=CuTokenBucket(_optional_number(f"{prefix}_RPC_MAX_CUPS")),
        block_time_store=block_time_store,
        batch_size=int(batch_size) if batch_size else DEFAULT_BATCH_SIZE,
    )
