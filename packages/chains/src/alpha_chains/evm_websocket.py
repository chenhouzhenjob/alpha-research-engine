"""EVM 链的 WebSocket 事件订阅。补充 `EvmChainAdapter`（纯 HTTP 轮询）做不到的"实时推送"能力，
用 web3.py 的 `AsyncWeb3(WebSocketProvider(...))` 订阅 `eth_subscribe("logs", ...)`。

阶段 0 范围缩减（已在实施计划里记录，不是遗漏）：只支持单个 WS 端点，不做 `EvmChainAdapter`
那样的多端点故障转移——连接断开时这个异步生成器直接结束，是否重连、重连后用现有
`ChainAdapter.get_logs`（HTTP）回填断线期间的区块区间，是调用方的职责，不在这一层做。

真实返回结构已用 NodeReal 的 WSS 端点对 BTC/USDT 池子实测验证过（不是照抄文档假设的字段名）：
`payload["result"]` 是一个 `AttributeDict`，`topics`/`data`/`transactionHash`/`blockHash` 是
`HexBytes`（`.hex()` 不带 `0x` 前缀，和 `evm_common._get_logs_chunk` 现有处理方式一致），
`blockNumber`/`logIndex`/`transactionIndex` 已经是解析好的 int，`address` 是校验和大小写的字符串
（需要 `.lower()`），另外还有一个标准 `eth_getLogs` 返回里没有的 `removed: bool` 字段——
链重组导致这条日志被撤销时会推送 `removed=True`，必须丢弃，不能当真实成交落库。

关于 `address` 过滤：已用只订阅 2 个池子（BTC/USDT + QQQB/USDT）实测验证过，NodeReal 这个
WSS 端点会正确按 `address` 过滤，收到的全部是订阅的那个池子的事件（第一次以为过滤失效，是因为
测试时误订阅了数据库里全部 28 个 qualified 候选池，看到多个地址的事件以为是"服务端没过滤"，
实际是"确实订阅了这 28 个地址"——排查过程记录在实施记录里，不是没验证过就下结论）。
这里仍然保留 `address` 客户端兜底过滤：不是因为已验证的这个 provider 有问题，而是不应该假设
换一个 RPC 提供商也会正确处理 `address` 这个订阅参数（服务端过滤行为不是 JSON-RPC 标准强制的），
这层过滤代价很低，防御性保留。

`blockTimestamp` 字段：NodeReal 的订阅 payload 里额外带了这个字段（标准 `eth_getLogs` 没有），
实测是形如 `'0x6a814e23'` 的十六进制字符串（Unix 秒），免费提供了出块时间，不需要再调
`get_block_timestamp` 现场查一次——这是 `LogEntry.block_time` 只在 WebSocket 路径填充的原因
（见 `base.py` 的字段注释）。这个字段不是标准 JSON-RPC 订阅结构的一部分，换 provider 可能没有，
所以按 `.get()` 处理、拿不到就留 `None`，不假设一定存在。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from web3 import AsyncWeb3, Web3
from web3.providers.persistent import WebSocketProvider

from .base import LogEntry

logger = logging.getLogger(__name__)


def _parse_block_timestamp(raw: object) -> datetime | None:
    """`blockTimestamp` 实测是十六进制字符串（Unix 秒），这里做个宽松解析——不是标准字段，
    换 provider 格式可能不同或干脆没有，解析失败就返回 None，不影响调用方回退到
    `get_block_timestamp`。
    """
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            return datetime.fromtimestamp(int(raw, 16), tz=UTC)
        if isinstance(raw, int):
            return datetime.fromtimestamp(raw, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return None


class EvmWebSocketSubscriber:
    """订阅指定合约地址 + topics 的日志事件，逐条产出 `LogEntry`。"""

    def __init__(self, wss_url: str) -> None:
        self._wss_url = wss_url

    async def subscribe_logs(self, *, address: str, topics: list[str]) -> AsyncIterator[LogEntry]:
        """持续产出匹配 `address` + `topics` 的日志，直到调用方停止迭代或连接断开。

        连接断开会让这个异步生成器直接抛出异常结束（`web3.py` 内部的连接管理行为）——
        重连、以及重连后怎么用 `get_logs` 回填断线期间的区块区间，由调用方负责
        （见模块文档"阶段 0 范围缩减"）。

        @param address 目标合约地址
        @param topics topic 过滤条件，位置对应 topic0..N（和 `ChainAdapter.get_logs` 同样的语义）
        @yields 匹配的日志，已经在这里做了两层过滤：`removed=True`（链重组撤销）的丢弃；
        `address` 不匹配的丢弃（防御性保留，见模块文档"关于 address 过滤"一节）
        """
        target_address = address.lower()
        async with AsyncWeb3(WebSocketProvider(self._wss_url)) as w3:
            await w3.eth.subscribe("logs", {"address": Web3.to_checksum_address(address), "topics": topics})
            async for payload in w3.socket.process_subscriptions():
                result = payload["result"]
                if result["address"].lower() != target_address:
                    continue
                if result["removed"]:
                    logger.warning(
                        "日志因链重组被撤销，丢弃: tx=%s log_index=%s",
                        result["transactionHash"].hex(),
                        result["logIndex"],
                    )
                    continue
                yield LogEntry(
                    address=result["address"].lower(),
                    topics=[t.hex() if hasattr(t, "hex") else t for t in result["topics"]],
                    data=result["data"].hex() if hasattr(result["data"], "hex") else result["data"],
                    block_number=result["blockNumber"],
                    log_index=result["logIndex"],
                    transaction_hash=(
                        result["transactionHash"].hex()
                        if hasattr(result["transactionHash"], "hex")
                        else result["transactionHash"]
                    ),
                    removed=False,
                    block_time=_parse_block_timestamp(result.get("blockTimestamp")),
                )
