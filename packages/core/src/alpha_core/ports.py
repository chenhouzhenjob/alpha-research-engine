"""跨包存储端口（Protocol）与它们交换的值对象。

依赖方向约束是 `alpha_chains`/`alpha_datasources` → `alpha_core`、`alpha_storage` → `alpha_core`，
链适配器和数据源不能直接依赖存储层。所以缓存接口定义在这里，由 `alpha_storage` 的仓储
按结构实现（不需要显式继承），调用方在组装时注入。不注入时各组件退回到原来的行为。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from .types import Chain


class BlockTimeSource(StrEnum):
    """区块时间的来源。"""

    RPC = "rpc"  # 专门调 eth_getBlockByNumber 查到的
    INDEXER = "indexer"  # 地址索引源的返回值里自带的
    WSS = "wss"  # WebSocket 推送 payload 里自带的 blockTimestamp
    RECEIPT = "receipt"  # 回执或其他已有数据里顺带得到的


class BlockTimeStore(Protocol):
    """区块时间的持久化缓存。区块时间不可变，写入后永久有效；严禁全量回填，只懒加载写入。"""

    def get_many(self, chain: Chain, block_numbers: list[int]) -> dict[int, datetime]:
        """批量读取；返回值只包含库里已有的区块，缺失的不出现在结果里。"""
        ...

    def put_many(self, chain: Chain, times: dict[int, datetime], source: BlockTimeSource) -> None:
        """批量写入；已存在的区块保留原值（不可变数据，不覆盖）。"""
        ...


class AbiKeyType(StrEnum):
    """ABI 缓存的查询键类型。"""

    ADDRESS = "address"  # 按合约地址查已验证的 ABI 和合约名
    FUNCTION = "function"  # 按 4 字节函数选择器查函数签名
    EVENT = "event"  # 按 topic0 查事件签名


class AbiStatus(StrEnum):
    """ABI 查询结果状态；not_found 和 invalid 是负缓存，到 retry_after 之前不重复查询。"""

    SUCCESS = "success"  # 查到了，永久有效
    NOT_FOUND = "not_found"  # 所有来源都明确表示没有
    INVALID = "invalid"  # 来源返回了数据，但无法解析


SIGNATURE_CHAIN = "*"  # 签名查询和链无关，AbiEntry.chain 记这个值


@dataclass(frozen=True)
class AbiEntry:
    """一条 ABI 或签名查询结果（包括负缓存）。"""

    chain: str  # 链标识；签名类查询记 SIGNATURE_CHAIN
    key_type: AbiKeyType
    key: str  # 小写带 0x 的地址、4 字节选择器或 topic0
    status: AbiStatus
    source: str | None  # 命中的来源（sourcify / openchain / 4byte）；not_found 时为 None
    name: str | None  # 合约名（地址类）或首选签名文本（签名类）；没有时为 None
    abi: list[Any] | None  # 地址类：ABI 数组；签名类：全部候选签名文本数组；非 success 时为 None
    fetched_at: datetime  # 查询时间（UTC）
    retry_after: datetime | None  # 负缓存到期时间；success 时为 None


class AbiStore(Protocol):
    """ABI 查询结果的持久化缓存。"""

    def get(self, chain: str, key_type: AbiKeyType, key: str) -> AbiEntry | None:
        """没有记录时返回 None；是否过期由调用方根据 retry_after 判断。"""
        ...

    def put(self, entry: AbiEntry) -> None:
        """写入或覆盖一条记录（负缓存到期后重查的结果需要覆盖旧记录）。"""
        ...


class PriceGranularity(StrEnum):
    """价格时间桶粒度。"""

    HOUR = "1h"  # 小时桶
    DAY = "1d"  # UTC 日桶


class PriceConfidence(StrEnum):
    """价格可信度，由数据源实现按取价方式给出，定价器据此决定是否参与汇总。"""

    HIGH = "high"  # 深度足够的池子或主流聚合价
    MEDIUM = "medium"  # 可用但深度一般
    LOW = "low"  # 浅池或推导价，只作参考


@dataclass(frozen=True)
class PricePoint:
    """某个 token 在某个时间桶的美元价格。"""

    chain: str
    token_address: str  # 小写带 0x
    granularity: PriceGranularity
    bucket_start: datetime  # 时间桶起点（UTC）
    price_usd: Decimal  # 该时间桶的收盘价（美元）
    source: str  # geckoterminal / coingecko
    source_ref: str | None  # 取价用的池子地址或 coin id；没有时为 None
    confidence: PriceConfidence
    reference_price_usd: Decimal | None = None  # 链下参考价（合成资产用）；一般为 None


class PriceStore(Protocol):
    """历史价格的持久化缓存；只缓存已经完全过去的时间桶（未收盘的桶会变）。"""

    def get(
        self, chain: str, token_address: str, granularity: PriceGranularity, bucket_start: datetime
    ) -> PricePoint | None: ...

    def put(self, point: PricePoint) -> None: ...


@dataclass(frozen=True)
class CachedState:
    """可变链上状态的一次读取结果。"""

    value: Any  # 可以 JSON 序列化的值
    block_number: int | None  # 读取时的区块号；未知时为 None
    fetched_at: datetime  # 读取时间（UTC）


class StateCache(Protocol):
    """可变状态（余额、slot0 等）的短时缓存；有效期由读取方决定。"""

    def get(self, chain: str, key: str, *, max_age_seconds: float) -> CachedState | None:
        """超过 max_age_seconds 的记录视为不存在，返回 None。"""
        ...

    def put(self, chain: str, key: str, value: Any, *, block_number: int | None) -> None: ...


class ReviewStatus(StrEnum):
    """合约识别结果的复核状态（设计文档 6.2）。"""

    AUTO = "auto"  # 自动识别（实例配置、注册表发现、CREATE2 校验、字节码），直接生效，可被新的自动结果刷新
    PENDING_REVIEW = "pending_review"  # LLM 判断的结果，报告里标注"待复核"
    CONFIRMED = "confirmed"  # 人工确认，任何自动流程都不能覆盖


@dataclass(frozen=True)
class ContractRecord:
    """一个地址的识别结果（contract_registry 的一行）。"""

    chain: str
    address: str  # 小写、带 0x
    kind: str  # 实例角色名（factory、router、market……）、pool / pair，或 eoa / unknown
    source: str  # 识别方式：static_roles / registry_call / known_table / create2 / code / llm / manual
    family: str | None = None  # 家族键；EOA 和未知合约为 None
    instance_key: str | None = None  # 实例键；未命名分叉为 None
    code_hash: str | None = None  # 运行时字节码的 keccak；没查过字节码为 None
    implementation_address: str | None = None  # 代理合约的实现地址（第二阶段）
    confidence: float = 1.0  # 0~1；确定性识别为 1.0
    review_status: ReviewStatus = ReviewStatus.AUTO
    evidence: dict[str, Any] = field(default_factory=dict)  # 识别依据，例如 CREATE2 的输入、注册表调用的返回


class ContractRegistryStore(Protocol):
    """合约识别结果的持久化。"""

    def get_many(self, chain: str, addresses: list[str]) -> dict[str, ContractRecord]:
        """返回已有的识别结果，键为小写地址；没有记录的地址不在结果里。"""
        ...

    def upsert_many(self, records: list[ContractRecord]) -> None:
        """写入识别结果；已是 confirmed 的记录不会被覆盖。"""
        ...
