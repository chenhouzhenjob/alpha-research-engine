"""EVM 系链的通用实现。新增 EVM 链继承本类，通常只需要覆盖构造参数（chain id、区块时间、
`eth_getLogs` 单次跨度上限等），不需要重写核心逻辑。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from alpha_core.errors import ChainAdapterError
from alpha_core.types import Chain
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from web3 import HTTPProvider, Web3
from web3.exceptions import Web3RPCError
from web3.middleware import ExtraDataToPOAMiddleware

from .base import ChainAdapter, LogEntry

logger = logging.getLogger(__name__)

# 多数 RPC 提供商对单次 eth_getLogs 的区块跨度有限制（常见 2000~10000），取保守默认值。
DEFAULT_LOG_CHUNK_SIZE = 2_000

_RETRYABLE_EXCEPTIONS = (Web3RPCError, ConnectionError, TimeoutError)


def _retrying():
    """RPC 调用的统一重试策略：指数退避，最多 5 次。"""
    return retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    )


class EvmChainAdapter(ChainAdapter):
    """基于 web3.py 的 EVM 链适配器，支持多个 RPC 端点故障转移（第一个为主，其余为兜底）。"""

    def __init__(
        self,
        chain: Chain,
        rpc_urls: list[str],
        *,
        log_chunk_size: int = DEFAULT_LOG_CHUNK_SIZE,
        is_poa: bool = False,
    ) -> None:
        if not rpc_urls:
            raise ChainAdapterError(f"{chain} 未配置任何 RPC 端点")
        self.chain = chain
        self._log_chunk_size = log_chunk_size
        self._clients = [Web3(HTTPProvider(url, request_kwargs={"timeout": 20})) for url in rpc_urls]
        if is_poa:
            # BSC 等 PoA/Clique 类共识链的区块头 extraData 超过标准 32 字节，
            # 不注入这个中间件会导致 eth_getBlock 解析直接抛 ExtraDataLengthError。
            for client in self._clients:
                client.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        # 区块时间戳一经查询即永久缓存（不可变数据只拉一次，见钱包链上行为分析设计方案 7.4）。
        self._block_timestamp_cache: dict[int, datetime] = {}

    def _with_failover(self, fn):
        """依次尝试各个 RPC 端点，全部失败才向上抛出，实现同一次调用的故障转移。"""
        last_error: Exception | None = None
        for client in self._clients:
            try:
                return fn(client)
            except _RETRYABLE_EXCEPTIONS as exc:  # noqa: PERF203 - 端点数量很小，性能可忽略
                last_error = exc
                logger.warning("RPC 端点调用失败，切换下一个: %s", exc)
        raise ChainAdapterError(f"{self.chain} 全部 RPC 端点均失败") from last_error

    @_retrying()
    def get_latest_block(self) -> int:
        return self._with_failover(lambda c: c.eth.block_number)

    @_retrying()
    def call(self, *, to: str, data: str) -> bytes:
        params = {"to": Web3.to_checksum_address(to), "data": data}
        result = self._with_failover(lambda c: c.eth.call(params))
        return bytes(result)

    def get_logs(
        self,
        *,
        address: str,
        topics: list[str | None],
        from_block: int,
        to_block: int,
    ) -> list[LogEntry]:
        """按 `_log_chunk_size` 分段拉取日志，对每一段做故障转移 + 重试。"""
        entries: list[LogEntry] = []
        cursor = from_block
        while cursor <= to_block:
            chunk_end = min(cursor + self._log_chunk_size - 1, to_block)
            entries.extend(self._get_logs_chunk(address, topics, cursor, chunk_end))
            cursor = chunk_end + 1
        return entries

    @_retrying()
    def _get_logs_chunk(
        self, address: str, topics: list[str | None], from_block: int, to_block: int
    ) -> list[LogEntry]:
        params = {
            "address": Web3.to_checksum_address(address),
            "topics": topics,
            "fromBlock": from_block,
            "toBlock": to_block,
        }
        raw_logs = self._with_failover(lambda c: c.eth.get_logs(params))
        return [
            LogEntry(
                address=log["address"].lower(),
                topics=[t.hex() if hasattr(t, "hex") else t for t in log["topics"]],
                data=log["data"].hex() if hasattr(log["data"], "hex") else log["data"],
                block_number=log["blockNumber"],
                log_index=log["logIndex"],
                transaction_hash=log["transactionHash"].hex(),
            )
            for log in raw_logs
        ]

    def get_block_timestamp(self, block_number: int) -> datetime:
        if block_number in self._block_timestamp_cache:
            return self._block_timestamp_cache[block_number]
        ts = self._fetch_block_timestamp(block_number)
        self._block_timestamp_cache[block_number] = ts
        return ts

    @_retrying()
    def _fetch_block_timestamp(self, block_number: int) -> datetime:
        block = self._with_failover(lambda c: c.eth.get_block(block_number))
        return datetime.fromtimestamp(block["timestamp"], tz=UTC)

    def find_block_by_timestamp(
        self, target: datetime, *, low: int = 0, high: int | None = None
    ) -> int:
        """二分查找第一个出块时间 >= `target` 的区块号，只依赖区块头时间戳，任何全节点都能查。"""
        hi = high if high is not None else self.get_latest_block()
        lo = low
        while lo < hi:
            mid = (lo + hi) // 2
            if self.get_block_timestamp(mid) >= target:
                hi = mid
            else:
                lo = mid + 1
        return hi
