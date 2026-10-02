"""把链画像、实例配置和家族组装成解码所需的上下文和解码器。

解码框架（`decoding/`）不依赖任何家族；家族依赖解码框架的接口。两者在这里接起来：
调用方（M3 的 ingest、测试、冒烟脚本）只需要给出链和交易，其余从配置推出来。

合约识别目前只有实例配置里写明的角色地址（识别第一层的 static_roles）；步骤 12 的识别 runner
会把 CREATE2、注册表调用等方式识别出的合约写进 contract_registry，调用方查出来后通过
`extra_identities` 传进来。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from alpha_core.chain_data import TxInfo, TxReceipt
from alpha_core.ports import AddressTransfer, ContractRecord, TransferKind
from alpha_core.types import Chain

from .config.chain_profiles import chain_profiles
from .config.instances import InstanceRegistry, instance_registry
from .decoding.context import ContractIdentity, DecodeContext
from .decoding.evm.dispatch import FamilyDecoder
from .decoding.evm.pipeline import decode_evm_transfers, decode_evm_tx
from .decoding.models import (
    DecodedTx,
    InternalTransfer,
    PositionCategory,
    PositionKind,
    PositionRef,
    TokenMeta,
)
from .families import FAMILIES
from .families.base import ProtocolFamily
from .valuation.dispatch import Reader, value_positions
from .valuation.models import PositionValuer, StateRead, Valuation, ValuationRequest


def identities_for(chain: Chain, registry: InstanceRegistry | None = None) -> dict[str, ContractIdentity]:
    """实例配置里写明的角色地址 → 识别结果。"""
    registry = registry or instance_registry()
    return {
        address: ContractIdentity(address, binding.role, binding.family, binding.instance_key)
        for (c, address), binding in registry.by_address.items()
        if c == chain
    }


def identities_from_records(records: Iterable[ContractRecord]) -> dict[str, ContractIdentity]:
    """把 contract_registry 的识别结果转成解码上下文用的识别结果（EOA 和未知合约不带家族）。"""
    return {r.address: ContractIdentity(r.address, r.kind, r.family, r.instance_key) for r in records}


def decoders_for(
    chain: Chain,
    registry: InstanceRegistry | None = None,
    families: Mapping[str, type[ProtocolFamily]] = FAMILIES,
) -> dict[str, FamilyDecoder]:
    """该链上全部实例部署的解码器，按实例键索引。"""
    registry = registry or instance_registry()
    return {d.instance_key: families[d.family].decoder(d) for d in registry.deployments_on(chain)}


def decode_context(
    chain: Chain,
    *,
    tokens: Mapping[str, TokenMeta] | None = None,
    contract_abis: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    event_signatures: Mapping[str, Sequence[str]] | None = None,
    extra_identities: Mapping[str, ContractIdentity] | None = None,
    registry: InstanceRegistry | None = None,
) -> DecodeContext:
    """组装解码上下文：基础资产来自链画像，合约识别来自实例配置（加上调用方给的其他识别结果）。"""
    identities = identities_for(chain, registry)
    identities.update(extra_identities or {})
    return DecodeContext(
        chain=chain.value,
        tokens=dict(tokens or {}),
        base_assets=chain_profiles()[chain].base_assets,
        identities=identities,
        contract_abis=dict(contract_abis or {}),
        event_signatures=dict(event_signatures or {}),
        wrapped_native=chain_profiles()[chain].wrapped_native,
        valuable_positions={k: v.position_kinds for k, v in valuers_for(chain, registry).items()},
    )


def decode_tx(
    chain: Chain,
    tx: TxInfo,
    receipt: TxReceipt,
    subject: str,
    *,
    ctx: DecodeContext | None = None,
    internal: Sequence[InternalTransfer] | None = None,
    registry: InstanceRegistry | None = None,
) -> DecodedTx:
    """用该链的链画像和全部已配置的实例解码一笔交易。

    @param ctx 解码上下文；不传时用 `decode_context(chain)`（没有 token 元数据和 ABI）
    @param internal 数据源给出的内部交易；None 表示数据源不可用
    """
    return decode_evm_tx(
        tx,
        receipt,
        subject,
        ctx or decode_context(chain, registry=registry),
        chain_profiles()[chain].rules,
        decoders=decoders_for(chain, registry),
        internal=internal,
    )


def decode_from_transfers(
    chain: Chain,
    tx: TxInfo,
    receipt: TxReceipt,
    transfers: Sequence[AddressTransfer],
    subject: str,
    *,
    ctx: DecodeContext | None = None,
    internal: Sequence[InternalTransfer] | None = None,
    registry: InstanceRegistry | None = None,
) -> DecodedTx:
    """索引路径：只凭地址索引源的转账解码一笔交易（T0），不需要回执日志。

    @param receipt 由索引字段构造的回执（status、gas_used、effective_gas_price；logs 为空）
    @param transfers 这笔交易里和钱包有关的转账（原生币条目忽略，内部转账用 internal 传）
    @param internal 内部交易；None 表示数据源不可用。可以用 `internal_from_transfers` 从索引结果转换
    """
    return decode_evm_transfers(
        tx,
        receipt,
        transfers,
        subject,
        ctx or decode_context(chain, registry=registry),
        chain_profiles()[chain].rules,
        internal=internal,
    )


def internal_from_transfers(transfers: Iterable[AddressTransfer]) -> list[InternalTransfer]:
    """从地址索引源 / 内部交易源的结果里取出内部转账，保持原顺序；回执路径和索引路径都用它组装 internal。"""
    return [
        InternalTransfer(t.from_address.lower(), t.to_address.lower(), t.amount_raw)
        for t in transfers
        if t.kind is TransferKind.INTERNAL and t.amount_raw > 0
    ]


def position_categories_for(
    chain: Chain,
    registry: InstanceRegistry | None = None,
    families: Mapping[str, type[ProtocolFamily]] = FAMILIES,
) -> dict[str, Mapping[PositionKind, PositionCategory]]:
    """该链上各实例的"持仓形态 → 持仓类型"映射，按实例键索引；没有持仓的家族映射为空。"""
    registry = registry or instance_registry()
    return {d.instance_key: families[d.family].position_categories for d in registry.deployments_on(chain)}


def position_category(
    position_key: str, categories: Mapping[str, Mapping[PositionKind, PositionCategory]]
) -> PositionCategory | None:
    """持仓键对应的持仓类型；实例未配置或家族没有声明这种形态时返回 None（分析层按未分类处理）。

    @param categories `position_categories_for` 的结果
    @raises ValueError 持仓键格式不对
    """
    kind = PositionRef.kind_of(position_key)  # 同时校验键的格式
    instance_key = position_key.split(":", 3)[1]
    return categories.get(instance_key, {}).get(kind)


def valuers_for(
    chain: Chain,
    registry: InstanceRegistry | None = None,
    families: Mapping[str, type[ProtocolFamily]] = FAMILIES,
) -> dict[str, PositionValuer]:
    """该链上所有可估值实例的估值器，按实例键索引。"""
    registry = registry or instance_registry()
    profile = chain_profiles()[chain]
    out: dict[str, PositionValuer] = {}
    for d in registry.deployments_on(chain):
        valuer = families[d.family].valuer(d, profile)
        if valuer is not None:
            out[d.instance_key] = valuer
    return out


def multicall_reader(adapter: Any, *, block: int | str = "latest") -> Reader:
    """用 Multicall3 执行状态读取的 reader；同一批在同一个区块上执行。

    @param adapter 支持 `raw_call(to, data, block)` 的链适配器
    """
    from alpha_chains.multicall import Call, multicall

    def read(reads: Sequence[StateRead]) -> dict[str, bytes | None]:
        results = multicall(adapter, [Call(r.to, bytes.fromhex(r.data[2:])) for r in reads], block=block)
        return {r.key: (res.data if res.success else None) for r, res in zip(reads, results, strict=True)}

    return read


def value(
    chain: Chain, requests: Sequence[ValuationRequest], reader: Reader, *, registry: InstanceRegistry | None = None
) -> list[Valuation]:
    """用该链上已配置的估值器估值一批持仓。"""
    return value_positions(requests, valuers_for(chain, registry), reader)
