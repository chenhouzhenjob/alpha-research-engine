"""`ChainAdapter` 抽象接口：新增一条链只需要实现这个接口，不改上层代码。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from alpha_core.types import Chain


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
        address: str,
        topics: list[str | None],
        from_block: int,
        to_block: int,
    ) -> list[LogEntry]:
        """按区块区间拉取指定合约地址、匹配给定 topics 的日志。

        实现方需要自行处理 RPC 提供商对单次查询区块跨度的限制（分段查询），
        调用方不需要关心分段细节。

        @param address 目标合约地址
        @param topics topic 过滤条件，位置对应 topic0..N；None 表示该位置不过滤
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
