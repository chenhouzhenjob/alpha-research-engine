"""识别第一层的执行（带 I/O，读取和字节码查询由调用方注入）。

识别顺序，越靠前越便宜，识别出来的永久写进 contract_registry，已有结果的地址不再处理：
1. 实例配置里写明的角色地址（零 RPC）；
2. 已有表（例如 V3 的 pool_candidates，由调用方以 `known_components` 提供盐的组成部分，本地 CREATE2 校验，零 RPC）；
3. CREATE2 校验：对剩下的地址批量读盐的组成部分（Multicall），本地算地址比对；
4. 字节码：剩下的批量 `eth_getCode`，为空是 EOA，非空记为 unknown（带 code_hash，交给第二阶段和 M4）。

注册表发现（Venus 的 getAllMarkets）是"一次列出全部"，由 `discover_registries` 单独执行。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from alpha_core.ports import ContractRecord, ContractRegistryStore
from alpha_core.types import Chain
from eth_utils import keccak

from ..config.chain_profiles import chain_profiles
from ..config.instances import InstanceRegistry, instance_registry
from ..families import FAMILIES
from ..families.base import ProtocolFamily
from ..valuation.models import StateRead
from .plans import Create2Rule, DiscoveryPlan

logger = logging.getLogger(__name__)

# 执行一批只读调用：StateRead → 返回数据（失败为 None）；与估值共用同一个 reader（runtime.multicall_reader）
Reader = Callable[[Sequence[StateRead]], dict[str, bytes | None]]
# 批量查询字节码：地址 → 字节码（0x 开头；EOA 为 "0x"）
CodeReader = Callable[[Sequence[str]], Mapping[str, str]]
# 已有表提供的盐的组成部分：(实例键, 地址) → 组成部分
KnownComponents = Mapping[tuple[str, str], tuple[Any, ...]]


def plans_for(
    chain: Chain,
    registry: InstanceRegistry | None = None,
    families: Mapping[str, type[ProtocolFamily]] = FAMILIES,
) -> list[DiscoveryPlan]:
    """该链上各实例部署声明的发现方式。"""
    registry = registry or instance_registry()
    profile = chain_profiles()[chain]
    return [p for d in registry.deployments_on(chain) if (p := families[d.family].discovery(d, profile)) is not None]


def discover_registries(
    chain: Chain,
    reader: Reader,
    *,
    registry: InstanceRegistry | None = None,
    families: Mapping[str, type[ProtocolFamily]] = FAMILIES,
) -> list[ContractRecord]:
    """执行各实例声明的注册表调用，返回发现的全部子合约（调用方负责写入 store）。"""
    calls = [c for p in plans_for(chain, registry, families) for c in p.registry_calls]
    results = reader([StateRead(f"registry:{c.instance_key}:{c.to}", c.to, c.data) for c in calls]) if calls else {}
    records = []
    for c in calls:
        raw = results.get(f"registry:{c.instance_key}:{c.to}")
        if raw is None:
            logger.warning("注册表调用失败：%s %s", c.instance_key, c.to)
            continue
        for address in c.decode(raw):
            records.append(
                ContractRecord(chain.value, address.lower(), c.kind, "registry_call", c.family, c.instance_key,
                               evidence={"registry": c.to})
            )  # fmt: skip
    return records


def identify(
    chain: Chain,
    addresses: Iterable[str],
    *,
    store: ContractRegistryStore,
    reader: Reader,
    code_reader: CodeReader,
    known_components: KnownComponents | None = None,
    registry: InstanceRegistry | None = None,
    families: Mapping[str, type[ProtocolFamily]] = FAMILIES,
) -> dict[str, ContractRecord]:
    """识别一批地址，把新结果写进 store，返回这批地址全部的识别结果（含已有的）。"""
    registry = registry or instance_registry()
    wanted = list(dict.fromkeys(a.lower() for a in addresses))
    known = store.get_many(chain.value, wanted)
    todo = [a for a in wanted if a not in known]
    found: dict[str, ContractRecord] = {}

    for a in todo:  # 1. 实例配置的角色地址
        binding = registry.by_address.get((chain, a))
        if binding is not None:
            found[a] = ContractRecord(
                chain.value, a, binding.role, "static_roles", binding.family, binding.instance_key
            )

    rules = [r for p in plans_for(chain, registry, families) for r in p.create2_rules]
    for (instance, a), values in (known_components or {}).items():  # 2. 已有表
        if a in todo and a not in found:
            for rule in (r for r in rules if r.instance_key == instance):
                if rule.matches(a, tuple(values)):
                    found[a] = _create2_record(chain, a, rule, tuple(values), "known_table")
                    break

    remaining = [a for a in todo if a not in found]  # 3. CREATE2 校验
    selector_sets = sorted({r.selectors for r in rules})
    reads = [StateRead(f"{a}:{sel}", a, sel) for a in remaining for sels in selector_sets for sel in sels]
    results = reader(list({r.key: r for r in reads}.values())) if reads else {}
    for a in remaining:
        for rule in rules:
            values = rule.decode([results.get(f"{a}:{sel}") for sel in rule.selectors])
            if values is not None and rule.matches(a, values):
                found[a] = _create2_record(chain, a, rule, values, "create2")
                break

    remaining = [a for a in todo if a not in found]  # 4. 字节码
    codes = code_reader(remaining) if remaining else {}
    for a in remaining:
        code = codes.get(a)
        if code is None:
            continue  # 查询失败：不记录，下次再识别
        if code in ("0x", ""):
            found[a] = ContractRecord(chain.value, a, "eoa", "code")
        else:
            found[a] = ContractRecord(chain.value, a, "unknown", "code", code_hash="0x" + keccak(hexstr=code).hex())

    if found:
        store.upsert_many(list(found.values()))
    return {**known, **found}


def _create2_record(chain: Chain, address: str, rule: Create2Rule, values: tuple, source: str) -> ContractRecord:
    evidence = {"deployer": rule.deployer, "components": [v.lower() if isinstance(v, str) else v for v in values]}
    return ContractRecord(chain.value, address, rule.kind, source, rule.family, rule.instance_key, evidence=evidence)
