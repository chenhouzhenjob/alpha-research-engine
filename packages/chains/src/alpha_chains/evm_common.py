"""EVM 系链的通用实现。新增 EVM 链继承本类，通常只需要覆盖构造参数（chain id、区块时间、
`eth_getLogs` 单次跨度上限等），不需要重写核心逻辑。

两条调用路径：
- **历史方法**（`get_latest_block`/`call`/`get_logs`/`get_block_timestamp`）继续走 web3 的类型化接口，
  输出格式保持不变（lp-backtest、live-signal 依赖），只在外面套上限速和记账。
- **钱包分析新增方法**（回执、交易、字节码、批量区块时间等）走自己实现的原始 JSON-RPC 通道
  （`_rpc`/`_rpc_batch`）：web3 的 HTTPProvider 自带一层对 HTTPError（含 429）的隐藏重试，
  既不计入账本，又会在配额耗尽时白白重试；自己实现才能精确控制重试、429 判定、批量和记账。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar

import requests
from alpha_core.errors import ChainAdapterError, RpcQuotaExhaustedError
from alpha_core.metering import CallMeter, CallStatus, NullCallMeter
from alpha_core.ports import BlockTimeSource, BlockTimeStore
from alpha_core.types import Chain
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from web3 import HTTPProvider, Web3
from web3.exceptions import Web3RPCError
from web3.middleware import ExtraDataToPOAMiddleware

from .base import BatchResult, ChainAdapter, LogEntry, RawLog, TopicFilter, TxInfo, TxReceipt
from .providers import cu_for, cu_for_rate_limit, detect_provider
from .rate_limit import CuTokenBucket

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 多数 RPC 提供商对单次 eth_getLogs 的区块跨度有限制（常见 2000~10000），取保守默认值。
DEFAULT_LOG_CHUNK_SIZE = 2_000
# 一次 JSON-RPC 批量请求最多包含的调用数；NodeReal 的上限未公开，取保守值，可由构造参数覆盖。
DEFAULT_BATCH_SIZE = 50

_RETRYABLE_EXCEPTIONS = (Web3RPCError, ConnectionError, TimeoutError)

# 故障转移（切到下一个端点）额外认的异常类型，比"同一端点值得重试"的 _RETRYABLE_EXCEPTIONS 更宽：
# HTTP 层错误（`requests.exceptions.HTTPError`，如 429/5xx）不属于瞬时抖动，重试同一个端点没有
# 意义（真实踩过：NodeReal 月度 CU 配额用完，返回 429，在下个计费周期重置前重试多少次都一样），
# 但换一个端点完全可能是好的——这里只加进故障转移的异常集合，不加进 _RETRYABLE_EXCEPTIONS，
# 避免在明知没用的同一个端点上先浪费几次重试才切换。
_FAILOVER_EXCEPTIONS = (*_RETRYABLE_EXCEPTIONS, requests.exceptions.HTTPError)

# JSON-RPC 错误信息里出现这些词，按"限流/配额耗尽"处理（各家措辞不统一，只能关键词匹配）。
_QUOTA_KEYWORDS = ("quota", "rate limit", "ratelimit", "too many requests", "exceeded the limit", "limit exceeded")


def _retrying():
    """历史方法的统一重试策略：指数退避，最多 5 次。"""
    return retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    )


class RpcResponseError(ChainAdapterError):
    """节点明确返回了 JSON-RPC 错误（如 execution reverted、参数非法）。

    这类错误是确定性的，换端点、重试都不会变，所以直接抛出，不做故障转移。
    `code`/`message` 保留原文，便于调用方区分（例如 Multicall 据此判断要不要拆小批次）。
    """

    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"{method} 返回错误 {code}: {message}")
        self.method = method
        self.code = code
        self.message = message


class _QuotaError(Exception):
    """内部标记：本端点限流或配额耗尽，应切换下一个端点。"""


class _TransientError(Exception):
    """内部标记：本端点瞬时故障（连接、超时、5xx），可以在同一端点短暂重试。"""


def _is_quota_message(message: str) -> bool:
    lowered = message.lower()
    return any(k in lowered for k in _QUOTA_KEYWORDS)


def _hex_to_int(value: str | None) -> int | None:
    return int(value, 16) if value is not None else None


def _lower(value: str | None) -> str | None:
    return value.lower() if value is not None else None


def _normalize_hash(tx_hash: str) -> str:
    h = tx_hash.lower()
    return h if h.startswith("0x") else "0x" + h


class _Endpoint:
    """一个 RPC 端点：web3 客户端（历史方法用）+ 原始 HTTP 会话（新方法用）+ 供应商标识。"""

    def __init__(self, url: str, *, is_poa: bool, timeout: float) -> None:
        self.url = url
        self.provider = detect_provider(url)
        self.web3 = Web3(HTTPProvider(url, request_kwargs={"timeout": 20}))
        if is_poa:
            # BSC 等 PoA/Clique 类共识链的区块头 extraData 超过标准 32 字节，
            # 不注入这个中间件会导致 eth_getBlock 解析直接抛 ExtraDataLengthError。
            self.web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.session = requests.Session()
        self.timeout = timeout

    def post(self, payload: dict | list) -> Any:
        """发送一次 JSON-RPC（单个或批量），返回解析后的 JSON；按错误类型抛内部标记异常。"""
        try:
            resp = self.session.post(self.url, json=payload, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise _TransientError(str(exc)) from exc
        if resp.status_code == 429:
            raise _QuotaError(f"HTTP 429: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise _TransientError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            text = resp.text[:200]
            if _is_quota_message(text):
                raise _QuotaError(f"HTTP {resp.status_code}: {text}")
            raise ChainAdapterError(f"{self.url} HTTP {resp.status_code}: {text}")
        return resp.json()


class EvmChainAdapter(ChainAdapter):
    """基于 web3.py 的 EVM 链适配器，支持多个 RPC 端点故障转移（第一个为主，其余为兜底）。"""

    def __init__(
        self,
        chain: Chain,
        rpc_urls: list[str],
        *,
        log_chunk_size: int = DEFAULT_LOG_CHUNK_SIZE,
        is_poa: bool = False,
        meter: CallMeter | None = None,
        rate_limiter: CuTokenBucket | None = None,
        block_time_store: BlockTimeStore | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        request_timeout: float = 20.0,
        transient_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        @param meter 外部调用计量器；不传则不记账（原有行为）
        @param rate_limiter CU 令牌桶；不传则不限速（原有行为）
        @param block_time_store 区块时间持久化缓存；不传则只缓存在进程内存（原有行为）
        @param batch_size 一次 JSON-RPC 批量请求的最大调用数
        @param transient_attempts 新方法在同一端点上对瞬时故障的最多尝试次数
        """
        if not rpc_urls:
            raise ChainAdapterError(f"{chain} 未配置任何 RPC 端点")
        self.chain = chain
        self._log_chunk_size = log_chunk_size
        self._endpoints = [_Endpoint(url, is_poa=is_poa, timeout=request_timeout) for url in rpc_urls]
        # 保留原属性名：历史代码和测试可能直接访问 _clients。
        self._clients = [e.web3 for e in self._endpoints]
        self._meter: CallMeter = meter or NullCallMeter()
        self._limiter = rate_limiter or CuTokenBucket(None)
        self._block_time_store = block_time_store
        self._batch_size = max(1, batch_size)
        self._transient_attempts = max(1, transient_attempts)
        self._sleep = sleep
        self._request_id = 0
        # 区块时间戳一经查询即永久缓存（不可变数据只拉一次，见钱包链上行为分析设计方案 7.4）。
        self._block_timestamp_cache: dict[int, datetime] = {}

    # ------------------------------------------------------------------
    # 历史方法的调用路径：web3 + 故障转移 + 限速 + 记账
    # ------------------------------------------------------------------

    def _with_failover(self, fn: Callable[[Web3], T], method: str | None = None) -> T:
        """依次尝试各个 RPC 端点，全部失败才向上抛出，实现同一次调用的故障转移。

        所有端点都是 HTTP 429 时抛 `RpcQuotaExhaustedError`（`ChainAdapterError` 的子类）。
        """
        last_error: Exception | None = None
        all_quota = True
        for endpoint in self._endpoints:
            if method is not None:
                self._limiter.acquire(cu_for_rate_limit(endpoint.provider, method))
            try:
                result = fn(endpoint.web3)
            except _FAILOVER_EXCEPTIONS as exc:  # noqa: PERF203 - 端点数量很小，性能可忽略
                last_error = exc
                is_quota = _is_http_429(exc)
                all_quota = all_quota and is_quota
                if method is not None:
                    status = CallStatus.RATE_LIMITED if is_quota else CallStatus.ERROR
                    self._meter.record(endpoint.provider, method, cu=cu_for(endpoint.provider, method), status=status)
                logger.warning("RPC 端点调用失败，切换下一个: %s", exc)
                continue
            if method is not None:
                self._meter.record(endpoint.provider, method, cu=cu_for(endpoint.provider, method))
            return result
        if all_quota and last_error is not None:
            raise RpcQuotaExhaustedError(f"{self.chain} 全部 RPC 端点均限流或配额耗尽") from last_error
        raise ChainAdapterError(f"{self.chain} 全部 RPC 端点均失败") from last_error

    @_retrying()
    def get_latest_block(self) -> int:
        return self._with_failover(lambda c: c.eth.block_number, "eth_blockNumber")

    @_retrying()
    def call(self, *, to: str, data: str) -> bytes:
        params = {"to": Web3.to_checksum_address(to), "data": data}
        result = self._with_failover(lambda c: c.eth.call(params), "eth_call")
        return bytes(result)

    def get_logs(
        self,
        *,
        address: str | list[str] | None,
        topics: list[TopicFilter],
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
        self, address: str | list[str] | None, topics: list[TopicFilter], from_block: int, to_block: int
    ) -> list[LogEntry]:
        params: dict[str, Any] = {"topics": topics, "fromBlock": from_block, "toBlock": to_block}
        if isinstance(address, str):
            params["address"] = Web3.to_checksum_address(address)
        elif address:
            params["address"] = [Web3.to_checksum_address(a) for a in address]
        raw_logs = self._with_failover(lambda c: c.eth.get_logs(params), "eth_getLogs")
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
        if self._block_time_store is not None:
            stored = self._block_time_store.get_many(self.chain, [block_number])
            if block_number in stored:
                self._block_timestamp_cache[block_number] = stored[block_number]
                return stored[block_number]
        ts = self._fetch_block_timestamp(block_number)
        self._block_timestamp_cache[block_number] = ts
        if self._block_time_store is not None:
            self._block_time_store.put_many(self.chain, {block_number: ts}, BlockTimeSource.RPC)
        return ts

    @_retrying()
    def _fetch_block_timestamp(self, block_number: int) -> datetime:
        block = self._with_failover(lambda c: c.eth.get_block(block_number), "eth_getBlockByNumber")
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

    # ------------------------------------------------------------------
    # 新调用路径：原始 JSON-RPC + 限速 + 故障转移 + 瞬时重试 + 记账
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post_with_retry(self, endpoint: _Endpoint, payload: dict | list, methods: Sequence[str]) -> Any:
        """在单个端点上发送请求，瞬时故障按指数退避重试；每次尝试都记账。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                return endpoint.post(payload)
            except _TransientError:
                self._record(endpoint, methods, CallStatus.ERROR)
                if attempt >= self._transient_attempts:
                    raise
                self._sleep(min(5.0, 0.5 * 2 ** (attempt - 1)))  # 0.5s、1s、2s……封顶 5s

    def _record(self, endpoint: _Endpoint, methods: Sequence[str], status: CallStatus) -> None:
        """按方法分组记账：批量请求里每个子调用都算一次。"""
        counts: dict[str, int] = {}
        for m in methods:
            counts[m] = counts.get(m, 0) + 1
        for m, n in counts.items():
            unit = cu_for(endpoint.provider, m)
            self._meter.record(endpoint.provider, m, count=n, cu=None if unit is None else unit * n, status=status)

    def _send(self, payload: dict | list, methods: Sequence[str]) -> Any:
        """依次尝试各端点发送一次请求（单个或批量），返回原始 JSON 响应。"""
        last_error: Exception | None = None
        all_quota = True
        for endpoint in self._endpoints:
            self._limiter.acquire(sum(cu_for_rate_limit(endpoint.provider, m) for m in methods))
            try:
                response = self._post_with_retry(endpoint, payload, methods)
            except _QuotaError as exc:
                self._record(endpoint, methods, CallStatus.RATE_LIMITED)
                last_error = exc
                logger.warning("RPC 端点限流或配额耗尽，切换下一个: %s %s", endpoint.provider, exc)
                continue
            except (_TransientError, ChainAdapterError) as exc:
                all_quota = False
                last_error = exc
                logger.warning("RPC 端点调用失败，切换下一个: %s %s", endpoint.provider, exc)
                continue
            # 单个请求返回的 JSON-RPC 错误如果是限流类，也按配额耗尽切换端点。
            if isinstance(response, dict) and "error" in response:
                message = str(response["error"].get("message", ""))
                if _is_quota_message(message):
                    self._record(endpoint, methods, CallStatus.RATE_LIMITED)
                    last_error = _QuotaError(message)
                    continue
            if isinstance(response, dict) and "error" not in response and not isinstance(payload, list):
                self._record(endpoint, methods, CallStatus.OK)
            return response, endpoint
        if all_quota and last_error is not None:
            raise RpcQuotaExhaustedError(f"{self.chain} 全部 RPC 端点均限流或配额耗尽") from last_error
        raise ChainAdapterError(f"{self.chain} 全部 RPC 端点均失败") from last_error

    def _rpc(self, method: str, params: list[Any]) -> Any:
        """发送单个 JSON-RPC 调用，返回 `result`；节点返回错误时抛 `RpcResponseError`。"""
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        response, endpoint = self._send(payload, [method])
        if "error" in response:
            self._record(endpoint, [method], CallStatus.ERROR)
            err = response["error"]
            raise RpcResponseError(method, err.get("code"), str(err.get("message", "")))
        return response.get("result")

    def _rpc_batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any | RpcResponseError]:
        """批量发送 JSON-RPC 调用，按 `batch_size` 分批；结果顺序与 `calls` 一致。

        单项返回错误时先单独重试一次（有些节点批量里偶发失败），仍失败则该位置放
        `RpcResponseError`，由调用方放进 `BatchResult.failed`，不静默丢弃。
        """
        results: list[Any | RpcResponseError] = [None] * len(calls)
        for start in range(0, len(calls), self._batch_size):
            chunk = calls[start : start + self._batch_size]
            ids = [self._next_id() for _ in chunk]
            payload = [
                {"jsonrpc": "2.0", "id": rid, "method": m, "params": p} for rid, (m, p) in zip(ids, chunk, strict=True)
            ]
            response, endpoint = self._send(payload, [m for m, _ in chunk])
            if isinstance(response, dict):
                # 有的节点对整个批量只返回一个错误对象（例如不支持批量）。
                message = str(response.get("error", {}).get("message", response))
                raise ChainAdapterError(f"批量请求被拒绝: {message}")
            by_id = {item.get("id"): item for item in response}
            ok_methods: list[str] = []
            for offset, (rid, (method, params)) in enumerate(zip(ids, chunk, strict=True)):
                item = by_id.get(rid)
                if item is not None and "error" not in item:
                    results[start + offset] = item.get("result")
                    ok_methods.append(method)
                    continue
                # 批量中的单项失败：单独重试一次。
                try:
                    results[start + offset] = self._rpc(method, params)
                except RpcResponseError as exc:
                    results[start + offset] = exc
            self._record(endpoint, ok_methods, CallStatus.OK)
        return results

    # ------------------------------------------------------------------
    # 钱包分析新增方法
    # ------------------------------------------------------------------

    def get_block_timestamps(self, block_numbers: list[int]) -> BatchResult[int, datetime]:
        result: BatchResult[int, datetime] = BatchResult()
        pending = [b for b in dict.fromkeys(block_numbers) if b not in self._block_timestamp_cache]
        for b in block_numbers:
            if b in self._block_timestamp_cache:
                result.ok[b] = self._block_timestamp_cache[b]
        if pending and self._block_time_store is not None:
            stored = self._block_time_store.get_many(self.chain, pending)
            self._block_timestamp_cache.update(stored)
            result.ok.update(stored)
            pending = [b for b in pending if b not in stored]
        if not pending:
            return result
        responses = self._rpc_batch([("eth_getBlockByNumber", [hex(b), False]) for b in pending])
        fetched: dict[int, datetime] = {}
        for block_number, resp in zip(pending, responses, strict=True):
            if isinstance(resp, RpcResponseError):
                result.failed[block_number] = str(resp)
            elif resp is None:
                result.failed[block_number] = "区块不存在"
            else:
                fetched[block_number] = datetime.fromtimestamp(int(resp["timestamp"], 16), tz=UTC)
        self._block_timestamp_cache.update(fetched)
        result.ok.update(fetched)
        if fetched and self._block_time_store is not None:
            self._block_time_store.put_many(self.chain, fetched, BlockTimeSource.RPC)
        return result

    def get_transaction_receipts(self, tx_hashes: list[str]) -> BatchResult[str, TxReceipt]:
        hashes = list(dict.fromkeys(_normalize_hash(h) for h in tx_hashes))
        responses = self._rpc_batch([("eth_getTransactionReceipt", [h]) for h in hashes])
        result: BatchResult[str, TxReceipt] = BatchResult()
        for tx_hash, resp in zip(hashes, responses, strict=True):
            if isinstance(resp, RpcResponseError):
                result.failed[tx_hash] = str(resp)
            elif resp is None:
                result.failed[tx_hash] = "回执不存在（交易未打包或哈希错误）"
            else:
                result.ok[tx_hash] = _parse_receipt(resp)
        return result

    def get_transactions(self, tx_hashes: list[str]) -> BatchResult[str, TxInfo]:
        hashes = list(dict.fromkeys(_normalize_hash(h) for h in tx_hashes))
        responses = self._rpc_batch([("eth_getTransactionByHash", [h]) for h in hashes])
        result: BatchResult[str, TxInfo] = BatchResult()
        for tx_hash, resp in zip(hashes, responses, strict=True):
            if isinstance(resp, RpcResponseError):
                result.failed[tx_hash] = str(resp)
            elif resp is None:
                result.failed[tx_hash] = "交易不存在"
            else:
                result.ok[tx_hash] = _parse_tx(resp)
        return result

    def get_codes(self, addresses: list[str]) -> BatchResult[str, str]:
        addrs = list(dict.fromkeys(a.lower() for a in addresses))
        responses = self._rpc_batch([("eth_getCode", [a, "latest"]) for a in addrs])
        result: BatchResult[str, str] = BatchResult()
        for addr, resp in zip(addrs, responses, strict=True):
            if isinstance(resp, RpcResponseError):
                result.failed[addr] = str(resp)
            else:
                result.ok[addr] = (resp or "0x").lower()
        return result

    def raw_call(self, *, to: str, data: str) -> bytes:
        """走新调用路径的 `eth_call`：节点返回的执行错误（revert、gas 超限）直接抛
        `RpcResponseError`，不重试也不切换端点。

        和历史方法 `call` 的区别：`call` 对 `Web3RPCError` 重试 5 次（指数退避），
        对确定性错误纯属浪费；Multicall 需要尽快拿到错误来决定是否拆小批次，所以用这个方法。
        """
        result = self._rpc("eth_call", [{"to": to.lower(), "data": data}, "latest"])
        return bytes.fromhex(str(result or "0x").removeprefix("0x"))

    def get_storage_at(self, address: str, slot: int) -> str:
        return str(self._rpc("eth_getStorageAt", [address.lower(), hex(slot), "latest"])).lower()

    def get_transaction_count(self, address: str) -> int:
        return int(self._rpc("eth_getTransactionCount", [address.lower(), "latest"]), 16)


def _is_http_429(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 429


def _parse_log(raw: dict[str, Any]) -> RawLog:
    return RawLog(
        address=raw["address"].lower(),
        topics=[t.lower() for t in raw.get("topics", [])],
        data=raw.get("data", "0x").lower(),
        log_index=int(raw["logIndex"], 16),
        block_number=int(raw["blockNumber"], 16),
        tx_hash=raw["transactionHash"].lower(),
    )


def _parse_receipt(raw: dict[str, Any]) -> TxReceipt:
    return TxReceipt(
        tx_hash=raw["transactionHash"].lower(),
        block_number=int(raw["blockNumber"], 16),
        tx_index=int(raw["transactionIndex"], 16),
        from_address=raw["from"].lower(),
        to_address=_lower(raw.get("to")),
        status=_hex_to_int(raw.get("status")),
        gas_used=int(raw["gasUsed"], 16),
        effective_gas_price=_hex_to_int(raw.get("effectiveGasPrice")),
        contract_address=_lower(raw.get("contractAddress")),
        logs=[_parse_log(log) for log in raw.get("logs", [])],
    )


def _parse_tx(raw: dict[str, Any]) -> TxInfo:
    return TxInfo(
        tx_hash=raw["hash"].lower(),
        block_number=_hex_to_int(raw.get("blockNumber")),
        tx_index=_hex_to_int(raw.get("transactionIndex")),
        from_address=raw["from"].lower(),
        to_address=_lower(raw.get("to")),
        value=int(raw.get("value", "0x0"), 16),
        input=(raw.get("input") or "0x").lower(),
        nonce=int(raw["nonce"], 16),
        gas_price=_hex_to_int(raw.get("gasPrice")),
    )
