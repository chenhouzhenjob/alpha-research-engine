"""Multicall3：把 N 个只读 `eth_call` 打包成一次调用。

按 NodeReal 的计价，一次 Multicall 按一次 `eth_call`（20 CU）计费，是真正减少额度消耗的手段
（JSON-RPC 批量请求仍按子调用分别计费，只省网络往返）。子调用很多时是否仍按一次计费，
待 M0 实测核实。

设计要点：
- 用 `tryAggregate(false, calls)`，每个子调用的成败单独返回；失败的子调用放进结果的
  `success=False`，**不会被当成 0**（DefiLlama 的 permitFailure 会静默吞掉失败，这里不照搬）。
- 批次大小自适应：整批执行失败（gas 超限、返回过大等）时对半拆开重试，最小拆到 1；
  配额耗尽（`RpcQuotaExhaustedError`）不拆，直接向上抛，由调用方暂停。
- Multicall3 在 BSC 上的部署地址已于 2026-09-26 用 eth_getCode 核实（3808 字节，
  包含 tryAggregate 和 getEthBalance 的函数选择器）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from alpha_core.errors import ChainAdapterError, RpcQuotaExhaustedError
from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError

from .base import BatchResult, BlockRef

logger = logging.getLogger(__name__)

MULTICALL3_ADDRESS = "0xca11bde05977b3631167028862be2a173976ca11"
_TRY_AGGREGATE_SELECTOR = bytes.fromhex("bce38bd7")  # tryAggregate(bool,(address,bytes)[])
_GET_ETH_BALANCE_SELECTOR = bytes.fromhex("4d2301cc")  # getEthBalance(address)

MULTICALL_CHUNK_ENV = "BNB_MULTICALL_CHUNK"
DEFAULT_MULTICALL_CHUNK = 400

K = TypeVar("K", bound=Hashable)


class SupportsRawCall(Protocol):
    """Multicall 需要的最小能力：一个遇到执行错误就立即抛出、不重试的 `eth_call`。"""

    def raw_call(self, *, to: str, data: str, block: BlockRef = "latest") -> bytes: ...


@dataclass(frozen=True)
class Call:
    """一个子调用。"""

    target: str  # 目标合约地址
    data: bytes  # ABI 编码后的调用数据（函数选择器 + 参数）


@dataclass(frozen=True)
class CallResult:
    """一个子调用的结果。"""

    success: bool  # 子调用是否执行成功
    data: bytes  # 返回数据；失败时为 revert 数据或空
    error: str | None = None  # 整批失败、拆到单个仍失败时的原因；子调用自身 revert 时为 None


def default_chunk_size() -> int:
    """默认批次大小，可用环境变量 `BNB_MULTICALL_CHUNK` 覆盖。"""
    raw = os.environ.get(MULTICALL_CHUNK_ENV, "").strip()
    return int(raw) if raw else DEFAULT_MULTICALL_CHUNK


def _encode_try_aggregate(calls: list[Call]) -> str:
    body = encode(["bool", "(address,bytes)[]"], [False, [(c.target, c.data) for c in calls]])
    return "0x" + (_TRY_AGGREGATE_SELECTOR + body).hex()


def _decode_try_aggregate(raw: bytes, expected: int) -> list[CallResult]:
    (items,) = decode(["(bool,bytes)[]"], raw)
    if len(items) != expected:
        raise ChainAdapterError(f"Multicall 返回 {len(items)} 个结果，期望 {expected} 个")
    return [CallResult(success=bool(ok), data=bytes(data)) for ok, data in items]


def multicall(
    adapter: SupportsRawCall, calls: list[Call], *, chunk_size: int | None = None, block: BlockRef = "latest"
) -> list[CallResult]:
    """执行一批只读子调用，结果顺序与 `calls` 一致。

    @param adapter 支持 `raw_call` 的链适配器
    @param calls 子调用列表
    @param chunk_size 初始批次大小；默认读 `BNB_MULTICALL_CHUNK`，未配置为 400
    @param block 在哪个区块之后的状态上执行；默认最新。同一批读取在同一个区块上，保证估值用到的状态彼此一致
    @returns 每个子调用的结果；整批失败且拆到单个仍失败的子调用 `success=False` 并带 `error`
    @raises RpcQuotaExhaustedError 配额耗尽，调用方应暂停
    """
    size = max(1, chunk_size or default_chunk_size())
    results: list[CallResult] = []
    for start in range(0, len(calls), size):
        results.extend(_run_chunk(adapter, calls[start : start + size], block))
    return results


def _run_chunk(adapter: SupportsRawCall, calls: list[Call], block: BlockRef) -> list[CallResult]:
    """执行一批；整批失败时对半拆分递归，直到单个子调用。"""
    try:
        raw = adapter.raw_call(to=MULTICALL3_ADDRESS, data=_encode_try_aggregate(calls), block=block)
        return _decode_try_aggregate(raw, len(calls))
    except RpcQuotaExhaustedError:
        raise
    except (ChainAdapterError, DecodingError) as exc:
        if len(calls) == 1:
            logger.warning("Multicall 单个子调用仍失败: target=%s %s", calls[0].target, exc)
            return [CallResult(success=False, data=b"", error=str(exc))]
        mid = (len(calls) + 1) // 2
        logger.info("Multicall 批次 %d 执行失败，对半拆分重试: %s", len(calls), exc)
        return _run_chunk(adapter, calls[:mid], block) + _run_chunk(adapter, calls[mid:], block)


def _decode_uint(data: bytes) -> int | None:
    if len(data) < 32:
        return None
    return int.from_bytes(data[:32], "big")


def get_eth_balances(
    adapter: SupportsRawCall, addresses: list[str], *, chunk_size: int | None = None
) -> BatchResult[str, int]:
    """批量读取原生币（BNB）余额，走 Multicall3 自带的 `getEthBalance`。键为小写地址。"""
    addrs = list(dict.fromkeys(a.lower() for a in addresses))
    calls = [Call(MULTICALL3_ADDRESS, _GET_ETH_BALANCE_SELECTOR + encode(["address"], [a])) for a in addrs]
    return _collect_uints(addrs, multicall(adapter, calls, chunk_size=chunk_size))


def _collect_uints(keys: list[K], results: list[CallResult]) -> BatchResult[K, int]:
    out: BatchResult[K, int] = BatchResult()
    for key, res in zip(keys, results, strict=True):
        value = _decode_uint(res.data) if res.success else None
        if value is None:
            out.failed[key] = res.error or ("子调用 revert" if not res.success else "返回数据长度不足")
        else:
            out.ok[key] = value
    return out
