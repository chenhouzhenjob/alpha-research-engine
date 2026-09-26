"""`ChainAdapter` 抽象接口：新增一条链只需要实现这个接口，不改上层代码。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Hashable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar

from alpha_core.chain_data import RawLog, TxInfo, TxReceipt
from alpha_core.types import Chain

__all__ = ["BatchResult", "ChainAdapter", "LogEntry", "RawLog", "TopicFilter", "TxInfo", "TxReceipt"]

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

# 日志 topic 过滤条件：某个位置可以是单个值、OR 数组（任一匹配），或 None（不过滤）。
TopicFilter = str | list[str] | None


@dataclass(frozen=True)
class LogEntry:
    """一条链上事件日志，字段与 `eth_getLogs` 返回结构一一对应，但不携带任何 web3 具体类型，
    保证 protocols/datasources 层不需要感知底层用的是哪个 RPC 客户端库。
    """

    address: str  # 触发事件的合约地址，小写
    topics: list[str]  # topic0..N，topic0 是事件签名哈希
    data: str  # 非 indexed 参数的 ABI 编码数据，0x 开头
    block_number: int
    log_index: int
    transaction_hash: str
    removed: bool = False  # 该日志是否因链重组被撤销；只有 WebSocket 订阅会推送 True
    # （eth_getLogs 按定义只返回查询时刻仍然有效的日志），调用方看到 True 必须丢弃，不能落库当真实成交
    block_time: datetime | None = None  # 出块时间；只有 WebSocket 订阅会免费带上这个字段
    # （NodeReal 的订阅 payload 里有 blockTimestamp，见 evm_websocket 模块文档），get_logs（HTTP）
    # 拿到的日志这里恒为 None——调用方需要时间戳又拿到 None，就该自己调 get_block_timestamp 补，
    # 不要假设这个字段一定有值


@dataclass
class BatchResult(Generic[K, V]):
    """批量读取的结果：成功的和失败的分开返回，失败项不会被静默丢弃或当成空值。"""

    ok: dict[K, V] = field(default_factory=dict)  # 键 → 读取结果
    failed: dict[K, str] = field(default_factory=dict)  # 键 → 失败原因


class ChainAdapter(ABC):
    """统一的链适配器接口。EVM 系新链通常继承 `evm_common.EvmChainAdapter`，只需覆盖差异项。"""

    chain: Chain

    @abstractmethod
    def get_latest_block(self) -> int:
        """返回链上最新（可能未终结）的区块号。"""

    @abstractmethod
    def get_logs(
        self,
        *,
        address: str | list[str] | None,
        topics: list[TopicFilter],
        from_block: int,
        to_block: int,
    ) -> list[LogEntry]:
        """按区块区间拉取指定合约地址、匹配给定 topics 的日志。

        实现方需要自行处理 RPC 提供商对单次查询区块跨度的限制（分段查询），
        调用方不需要关心分段细节。

        @param address 目标合约地址；传列表表示任一地址；传 None 表示不限地址（部分 RPC 会拒绝）
        @param topics topic 过滤条件，位置对应 topic0..N；某个位置传列表表示 OR，None 表示该位置不过滤
        @param from_block 起始区块（含）
        @param to_block 结束区块（含）
        @returns 匹配的日志列表，按区块号、log_index 升序
        """

    @abstractmethod
    def get_block_timestamp(self, block_number: int) -> datetime:
        """返回指定区块的出块时间（UTC）。"""

    @abstractmethod
    def call(self, *, to: str, data: str) -> bytes:
        """执行一次只读 `eth_call`，返回原始返回数据（未做任何 ABI 解码）。

        用于"已经知道具体合约地址+具体参数，只想读一次当前状态"的场景（比如 Factory.getPool
        按 token 对直接查池子地址），比扫描历史事件日志便宜得多，但只能回答"现在是什么"，
        不能像事件扫描一样发现"还不知道地址的东西"。

        @param to 目标合约地址
        @param data ABI 编码后的调用数据（4 字节函数选择器 + 参数），0x 开头
        @returns 原始返回数据
        """

    @abstractmethod
    def get_block_timestamps(self, block_numbers: list[int]) -> BatchResult[int, datetime]:
        """批量返回多个区块的出块时间（UTC）。已缓存的不发请求。"""

    @abstractmethod
    def get_transaction_receipts(self, tx_hashes: list[str]) -> BatchResult[str, TxReceipt]:
        """按交易哈希批量取回执；键为小写带 0x 的哈希。查不到的交易放进 `failed`。"""

    @abstractmethod
    def get_transactions(self, tx_hashes: list[str]) -> BatchResult[str, TxInfo]:
        """按交易哈希批量取交易本身（调用数据、原生币数量等）。"""

    @abstractmethod
    def get_codes(self, addresses: list[str]) -> BatchResult[str, str]:
        """批量取合约当前的 runtime bytecode；EOA 返回 "0x"。键为小写地址。"""

    @abstractmethod
    def get_storage_at(self, address: str, slot: int) -> str:
        """读取合约某个存储槽的当前值（32 字节，0x 开头），用于识别代理合约的实现地址。"""

    @abstractmethod
    def get_transaction_count(self, address: str) -> int:
        """返回地址当前的 nonce（已发出的交易数），用于核对索引源的数据是否完整。"""

    @abstractmethod
    def find_block_by_timestamp(self, target: datetime, *, low: int = 0, high: int | None = None) -> int:
        """二分查找第一个出块时间 >= `target` 的区块号。

        用途:确定 Factory 之类合约的历史扫描起点时,不能用"二分查找字节码首次非空"那套做法——
        `eth_getCode` 在任意历史区块查询需要该区块的完整 state trie,普通全节点/公共 RPC
        大多只保留近期 state(archive node 才有完整历史 state,成本高,见
        pool-discovery-metrics-v1.md 对 TickLens 历史快照同类限制的讨论)。
        区块时间戳则不同,任何全节点都能查,不需要 archive。所以扫描起点改用
        "协议已知不早于某个日期上线"这个先验时间锚点,二分定位到对应区块,
        再用普通的 `get_logs` 向前扫,不依赖任何历史 state 查询。

        @param target 目标时间(UTC)
        @param low 二分下界，默认从创世区块开始
        @param high 二分上界，默认取链上最新区块
        @returns 首个出块时间 >= target 的区块号
        """
