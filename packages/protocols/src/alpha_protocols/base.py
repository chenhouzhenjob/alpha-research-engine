"""`FactoryDiscoveryPlugin` 接口：声明协议如何从 Factory 事件里发现全量池子。

本期只需要"工厂自动发现"这一层（对应钱包链上行为分析设计方案 3.2 第一层），
字节码指纹/ABI 库/未知合约待办队列留待后续协议识别需求出现时再补。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from alpha_chains.base import LogEntry
from alpha_core.models import PoolCandidate
from alpha_core.types import Chain, DexId


class FactoryDiscoveryPlugin(ABC):
    """一个"能从 Factory 事件里展开全量池子"的协议实现。"""

    chain: Chain
    dex_id: DexId
    factory_address: str  # Factory 合约地址，人工只需要录入这一个种子地址

    @abstractmethod
    def pool_created_topic0(self) -> str:
        """`PoolCreated` 类事件的 topic0（事件签名哈希），用于 `eth_getLogs` 过滤。"""

    @abstractmethod
    def decode_pool_created(self, log: LogEntry) -> PoolCandidate:
        """把一条 `PoolCreated` 原始日志解码成 `PoolCandidate`（不含区块时间戳，懒加载补齐）。"""

    @abstractmethod
    def launch_date_hint_utc(self) -> datetime:
        """协议已知不早于这个日期上线（留了安全余量），用于首次扫描时定位起始区块。

        不是精确部署时间——只需要保证"真实部署时间一定晚于这个日期"，
        由 `ChainAdapter.find_block_by_timestamp` 二分定位到对应区块后，再正常向前扫描 `PoolCreated`，
        不依赖任何需要 archive node 的历史 state 查询。
        """
