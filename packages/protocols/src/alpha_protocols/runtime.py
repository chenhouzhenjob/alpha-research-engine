"""把链画像、实例配置和家族组装成解码所需的上下文和解码器。

解码框架（`decoding/`）不依赖任何家族；家族依赖解码框架的接口。两者在这里接起来：
调用方（M3 的 ingest、测试、冒烟脚本）只需要给出链和交易，其余从配置推出来。

合约识别目前只有实例配置里写明的角色地址（识别第一层的 static_roles）；步骤 12 的识别 runner
会把 CREATE2、注册表调用等方式识别出的合约写进 contract_registry，调用方查出来后通过
`extra_identities` 传进来。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from alpha_core.chain_data import TxInfo, TxReceipt
from alpha_core.types import Chain

from .config.chain_profiles import chain_profiles
from .config.instances import InstanceRegistry, instance_registry
from .decoding.context import ContractIdentity, DecodeContext
from .decoding.evm.dispatch import FamilyDecoder
from .decoding.evm.pipeline import decode_evm_tx
from .decoding.models import DecodedTx, InternalTransfer, TokenMeta
from .families import FAMILIES
from .families.base import ProtocolFamily


def identities_for(chain: Chain, registry: InstanceRegistry | None = None) -> dict[str, ContractIdentity]:
    """实例配置里写明的角色地址 → 识别结果。"""
    registry = registry or instance_registry()
    return {
        address: ContractIdentity(address, binding.role, binding.family, binding.instance_key)
        for (c, address), binding in registry.by_address.items()
        if c == chain
    }


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
