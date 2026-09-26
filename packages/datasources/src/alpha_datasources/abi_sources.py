"""合约 ABI 与函数/事件签名的多来源解析（协议识别第三层，见钱包链上行为分析设计方案 3.2）。

- 按地址查：Sourcify v2（已验证合约的 ABI 和合约名）。
- 按签名查：openchain → 4byte（按函数选择器或事件 topic0 反查签名文本）。

每次查询结果（包括"查不到"）都写入 `AbiStore` 负缓存，到期前不重复查询；临时错误不写缓存，
避免把"暂时查不到"误记成"查不到"。每个来源有独立的熔断器，连续失败时冷却期内直接跳过。
接口返回格式已于 2026-09-26 实测（见各类的注释），不是照抄文档。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import requests
from alpha_core.errors import DataSourceUnavailableError
from alpha_core.metering import CallMeter
from alpha_core.ports import SIGNATURE_CHAIN, AbiEntry, AbiKeyType, AbiStatus, AbiStore
from alpha_core.types import EVM_CHAIN_IDS, Chain

from ._http import CircuitBreaker, Throttle, metered_get

logger = logging.getLogger(__name__)

# 负缓存有效期：合约可能之后才验证源码，所以地址类较短；签名库收录新签名很慢，签名类较长。
NOT_FOUND_TTL_ADDRESS = timedelta(days=7)
NOT_FOUND_TTL_SIGNATURE = timedelta(days=30)
INVALID_TTL = timedelta(days=30)


class LookupKind(StrEnum):
    """单个来源的查询结论。"""

    FOUND = "found"  # 查到
    NOT_FOUND = "not_found"  # 明确没有
    INVALID = "invalid"  # 返回了数据但无法解析


@dataclass(frozen=True)
class LookupResult:
    """单个来源的查询结果。"""

    kind: LookupKind
    name: str | None = None  # 合约名或首选签名
    abi: list[Any] | None = None  # 地址类：ABI；签名类：候选签名（首选排第一）


class AbiSource(Protocol):
    """一个 ABI/签名来源。临时故障抛 `DataSourceUnavailableError`（含限流子类）。"""

    name: str

    def supports(self, key_type: AbiKeyType) -> bool: ...

    def lookup(self, chain: Chain, key_type: AbiKeyType, key: str) -> LookupResult: ...


class SourcifySource:
    """Sourcify v2：`GET /server/v2/contract/{chainId}/{address}?fields=abi,compilation`。

    实测：已验证返回 200，`compilation.name` 是合约名；未验证返回 404 且 `match` 为 null。
    """

    name = "sourcify"
    BASE_URL = "https://sourcify.dev/server/v2/contract"

    def __init__(self, *, session: requests.Session | None = None, meter: CallMeter | None = None) -> None:
        self._session = session or requests.Session()
        self._meter = meter
        self._throttle = Throttle(0.2)

    def supports(self, key_type: AbiKeyType) -> bool:
        return key_type == AbiKeyType.ADDRESS

    def lookup(self, chain: Chain, key_type: AbiKeyType, key: str) -> LookupResult:
        resp = metered_get(
            self._session,
            f"{self.BASE_URL}/{EVM_CHAIN_IDS[chain]}/{key}",
            provider=self.name,
            method="/v2/contract",
            params={"fields": "abi,compilation"},
            meter=self._meter,
            throttle=self._throttle,
        )
        if resp.status_code == 404:
            return LookupResult(LookupKind.NOT_FOUND)
        if resp.status_code != 200:
            raise DataSourceUnavailableError(f"sourcify HTTP {resp.status_code}")
        try:
            body = resp.json()
            abi = body["abi"]
            if not isinstance(abi, list):
                return LookupResult(LookupKind.INVALID)
            return LookupResult(LookupKind.FOUND, name=(body.get("compilation") or {}).get("name"), abi=abi)
        except (ValueError, KeyError):
            return LookupResult(LookupKind.INVALID)


class OpenchainSource:
    """openchain 签名库：`GET /signature-database/v1/lookup?function=..&event=..&filter=false`。

    实测：`result.function[选择器]` 为候选数组，每项带 `filtered`（疑似垃圾）和 `hasVerifiedContract`。
    用 `filter=false` 取回全部候选（碰撞签名也保留），自己排序：首选"未被过滤且有已验证合约"的那条。
    """

    name = "openchain"
    URL = "https://api.openchain.xyz/signature-database/v1/lookup"

    def __init__(self, *, session: requests.Session | None = None, meter: CallMeter | None = None) -> None:
        self._session = session or requests.Session()
        self._meter = meter
        self._throttle = Throttle(0.2)

    def supports(self, key_type: AbiKeyType) -> bool:
        return key_type in (AbiKeyType.FUNCTION, AbiKeyType.EVENT)

    def lookup(self, chain: Chain, key_type: AbiKeyType, key: str) -> LookupResult:
        field = "function" if key_type == AbiKeyType.FUNCTION else "event"
        resp = metered_get(
            self._session,
            self.URL,
            provider=self.name,
            method="/lookup",
            params={field: key, "filter": "false"},
            meter=self._meter,
            throttle=self._throttle,
        )
        if resp.status_code != 200:
            raise DataSourceUnavailableError(f"openchain HTTP {resp.status_code}")
        try:
            items = (resp.json().get("result") or {}).get(field, {}).get(key) or []
        except (ValueError, AttributeError):
            return LookupResult(LookupKind.INVALID)
        if not items:
            return LookupResult(LookupKind.NOT_FOUND)
        ranked = sorted(items, key=lambda i: (bool(i.get("filtered")), not i.get("hasVerifiedContract", False)))
        names = [i["name"] for i in ranked if i.get("name")]
        return LookupResult(LookupKind.FOUND, name=names[0], abi=names) if names else LookupResult(LookupKind.INVALID)


class FourByteSource:
    """4byte：`/api/v1/signatures/?hex_signature=`（函数）与 `/api/v1/event-signatures/?hex_signature=`（事件）。

    实测：结果按创建时间倒序，碰撞出来的垃圾签名排在前面，真正的签名通常是 id 最小（最早）的那条，
    所以按 id 升序排列，首选最早的。
    """

    name = "4byte"
    BASE_URL = "https://www.4byte.directory/api/v1"

    def __init__(self, *, session: requests.Session | None = None, meter: CallMeter | None = None) -> None:
        self._session = session or requests.Session()
        self._meter = meter
        self._throttle = Throttle(0.5)

    def supports(self, key_type: AbiKeyType) -> bool:
        return key_type in (AbiKeyType.FUNCTION, AbiKeyType.EVENT)

    def lookup(self, chain: Chain, key_type: AbiKeyType, key: str) -> LookupResult:
        path = "/signatures/" if key_type == AbiKeyType.FUNCTION else "/event-signatures/"
        resp = metered_get(
            self._session,
            self.BASE_URL + path,
            provider=self.name,
            method=path,
            params={"hex_signature": key},
            meter=self._meter,
            throttle=self._throttle,
        )
        if resp.status_code != 200:
            raise DataSourceUnavailableError(f"4byte HTTP {resp.status_code}")
        try:
            results = resp.json().get("results") or []
            names = [r["text_signature"] for r in sorted(results, key=lambda r: r.get("id", 0))]
        except (ValueError, KeyError, AttributeError):
            return LookupResult(LookupKind.INVALID)
        return LookupResult(LookupKind.FOUND, name=names[0], abi=names) if names else LookupResult(LookupKind.NOT_FOUND)


class AbiResolver:
    """按"缓存 → 各来源依次尝试 → 写缓存"解析 ABI 或签名。"""

    def __init__(
        self,
        sources: list[AbiSource],
        *,
        store: AbiStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        breaker_factory: Callable[[], CircuitBreaker] = CircuitBreaker,
    ) -> None:
        self._sources = sources
        self._store = store
        self._clock = clock
        self._breakers = {s.name: breaker_factory() for s in sources}

    def resolve(self, chain: Chain, key_type: AbiKeyType, key: str) -> AbiEntry:
        """返回解析结果（success / not_found / invalid）。

        @raises DataSourceUnavailableError 缓存未命中，且所有可用来源都是临时故障（结果不写缓存）
        """
        cache_chain = chain.value if key_type == AbiKeyType.ADDRESS else SIGNATURE_CHAIN
        key = key.lower()
        now = self._clock()
        if self._store is not None:
            cached = self._store.get(cache_chain, key_type, key)
            if cached is not None and (
                cached.status == AbiStatus.SUCCESS or (cached.retry_after is not None and cached.retry_after > now)
            ):
                return cached

        saw_invalid = False
        saw_definitive = False
        transient_errors: list[str] = []
        for source in (s for s in self._sources if s.supports(key_type)):
            breaker = self._breakers[source.name]
            if not breaker.allow():
                transient_errors.append(f"{source.name}: 熔断中")
                continue
            try:
                result = source.lookup(chain, key_type, key)
            except DataSourceUnavailableError as exc:
                breaker.on_failure()
                transient_errors.append(f"{source.name}: {exc}")
                logger.warning("ABI 来源 %s 查询失败: %s", source.name, exc)
                continue
            breaker.on_success()
            saw_definitive = True
            if result.kind == LookupKind.FOUND:
                return self._save(
                    AbiEntry(
                        cache_chain, key_type, key, AbiStatus.SUCCESS, source.name, result.name, result.abi, now, None
                    )
                )
            saw_invalid = saw_invalid or result.kind == LookupKind.INVALID

        if not saw_definitive:
            raise DataSourceUnavailableError(
                f"ABI 来源均不可用: {'; '.join(transient_errors) or '没有支持该类型的来源'}"
            )
        if saw_invalid:
            status, ttl = AbiStatus.INVALID, INVALID_TTL
        else:
            status = AbiStatus.NOT_FOUND
            ttl = NOT_FOUND_TTL_ADDRESS if key_type == AbiKeyType.ADDRESS else NOT_FOUND_TTL_SIGNATURE
        return self._save(AbiEntry(cache_chain, key_type, key, status, None, None, None, now, now + ttl))

    def _save(self, entry: AbiEntry) -> AbiEntry:
        if self._store is not None:
            self._store.put(entry)
        return entry


def default_resolver(*, store: AbiStore | None = None, meter: CallMeter | None = None) -> AbiResolver:
    """默认组合：Sourcify（地址）+ openchain → 4byte（签名）。"""
    session = requests.Session()
    return AbiResolver(
        [
            SourcifySource(session=session, meter=meter),
            OpenchainSource(session=session, meter=meter),
            FourByteSource(session=session, meter=meter),
        ],
        store=store,
    )
