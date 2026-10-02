"""`alpha_core.ports` 各存储端口的数据库实现，供链适配器和数据源注入使用。

仓储（repositories）绑定一个调用方管理的 Session，适合在事务里批量读写；而链适配器、
数据源这类长生命周期对象在任意时刻读写缓存，没有现成的事务可用。这里的实现每次调用
开一个短事务（`session_scope`），写入立即提交，保证缓存结果不会因为调用方后续出错而丢失。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from alpha_core.ports import (
    AbiEntry,
    AbiKeyType,
    BlockTimeSource,
    CachedState,
    ContractRecord,
    PriceGranularity,
    PricePoint,
)
from alpha_core.types import Chain

from .db import session_scope
from .repositories.block_times import BlockTimeRepository
from .repositories.caches import AbiCacheRepository, ChainStateCacheRepository, PricePointRepository
from .repositories.contract_registry import ContractRegistryRepository


class DbBlockTimeStore:
    """`BlockTimeStore` 的数据库实现。"""

    def get_many(self, chain: Chain, block_numbers: list[int]) -> dict[int, datetime]:
        with session_scope() as s:
            return BlockTimeRepository(s).get_many(chain, block_numbers)

    def put_many(self, chain: Chain, times: dict[int, datetime], source: BlockTimeSource) -> None:
        with session_scope() as s:
            BlockTimeRepository(s).put_many(chain, times, source)


class DbAbiStore:
    """`AbiStore` 的数据库实现。"""

    def get(self, chain: str, key_type: AbiKeyType, key: str) -> AbiEntry | None:
        with session_scope() as s:
            return AbiCacheRepository(s).get(chain, key_type, key)

    def put(self, entry: AbiEntry) -> None:
        with session_scope() as s:
            AbiCacheRepository(s).put(entry)


class DbPriceStore:
    """`PriceStore` 的数据库实现。"""

    def get(
        self, chain: str, token_address: str, granularity: PriceGranularity, bucket_start: datetime
    ) -> PricePoint | None:
        with session_scope() as s:
            return PricePointRepository(s).get(chain, token_address, granularity, bucket_start)

    def put(self, point: PricePoint) -> None:
        with session_scope() as s:
            PricePointRepository(s).put(point)


class DbStateCache:
    """`StateCache` 的数据库实现。"""

    def get(self, chain: str, key: str, *, max_age_seconds: float) -> CachedState | None:
        with session_scope() as s:
            return ChainStateCacheRepository(s).get(chain, key, max_age_seconds=max_age_seconds)

    def put(self, chain: str, key: str, value: Any, *, block_number: int | None) -> None:
        with session_scope() as s:
            ChainStateCacheRepository(s).put(chain, key, value, block_number=block_number)


class DbContractRegistryStore:
    """`ContractRegistryStore` 的数据库实现。"""

    def get_many(self, chain: str, addresses: list[str]) -> dict[str, ContractRecord]:
        with session_scope() as s:
            return ContractRegistryRepository(s).get_many(chain, addresses)

    def upsert_many(self, records: list[ContractRecord]) -> None:
        with session_scope() as s:
            ContractRegistryRepository(s).upsert_many(records)
