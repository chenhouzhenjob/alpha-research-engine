"""链画像：每条链一份 `chains/<chain>.yaml`，声明这条链与别的链不同的地方（规划 5.10、5.12）。

新增一条 EVM 链：在 `alpha_core.types` 登记链和链规格，再新增一份链画像，解码框架不用改。
链画像只放解码和估值用到的差异；chainId、RPC 环境变量等适配器需要的信息在 `alpha_core.types.CHAIN_SPECS`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from alpha_core.types import CHAIN_SPECS, Chain
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..decoding.evm.rules import ChainRules, Create2Variant, GasModel, SystemTxRule
from ..decoding.models import NATIVE
from ._common import Address, ConfigError, package_dir, read_yaml

# 基础资产清单里原生币的地址写法
NATIVE_ADDRESS = "native"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeSpec(_Strict):
    """原生币。"""

    symbol: str  # 例如 BNB、ETH
    decimals: int = Field(ge=0, le=36)  # 精度位数


class AssetSpec(_Strict):
    """一个基础资产：风险标记（识别仿冒）和 M3 定价、跨链汇总都依赖这份清单。"""

    asset_id: str = Field(pattern=r"^[a-z0-9]+$")  # 跨链统一的经济资产编号：WBNB 和 BNB 都是 bnb，各链 USDT 都是 usdt
    address: Address | Literal["native"]  # token 地址；原生币写 native
    symbol: str | None = None  # token 的 symbol；原生币不填，取 native.symbol
    decimals: int | None = Field(default=None, ge=0, le=36)  # 精度；原生币不填。同一资产在不同链上精度可能不同


class ChainProfileFile(_Strict):
    """链画像文件的 schema。"""

    chain: Chain
    vm: Literal["evm"]  # 虚拟机类型；目前只有 EVM，接入非 EVM 链时扩展
    native: NativeSpec
    wrapped_native: Address  # 包装原生币（WBNB、WETH）地址
    gas_model: GasModel
    create2_variant: Create2Variant
    system_tx_rules: list[SystemTxRule] = []
    provider_slugs: dict[str, str] = {}  # 数据供应商对这条链的叫法，例如 {ankr: eth}
    assets: list[AssetSpec]

    @model_validator(mode="after")
    def _check(self) -> ChainProfileFile:
        natives = [a for a in self.assets if a.address == NATIVE_ADDRESS]
        if len(natives) != 1:
            raise ValueError("assets 里必须恰好有一个 address: native 的原生币")
        if natives[0].symbol is not None or natives[0].decimals is not None:
            raise ValueError("原生币的 symbol、decimals 写在 native 里，assets 里不要重复写")
        tokens = [a for a in self.assets if a.address != NATIVE_ADDRESS]
        if any(a.symbol is None or a.decimals is None for a in tokens):
            raise ValueError("token 资产必须写 symbol 和 decimals")
        addresses = [a.address for a in tokens]
        if len(addresses) != len(set(addresses)):
            raise ValueError("assets 里有重复地址")
        if self.wrapped_native not in addresses:
            raise ValueError("wrapped_native 必须也列在 assets 里")
        if natives[0].asset_id != next(a.asset_id for a in tokens if a.address == self.wrapped_native):
            raise ValueError("包装原生币和原生币的 asset_id 必须相同")
        return self


@dataclass(frozen=True)
class ChainProfile:
    """加载后的链画像。"""

    chain: Chain
    vm: str
    native_symbol: str
    native_decimals: int
    wrapped_native: str
    rules: ChainRules  # gas 模型和系统交易规则，交给 EVM 解码层
    create2_variant: Create2Variant
    provider_slugs: Mapping[str, str]
    assets: tuple[AssetSpec, ...]

    @property
    def base_assets(self) -> dict[str, str]:
        """解码上下文用的基础资产清单：地址（原生币为 NATIVE）→ symbol。"""
        out = {NATIVE: self.native_symbol}
        out.update({a.address: a.symbol for a in self.assets if a.address != NATIVE_ADDRESS and a.symbol})
        return out

    def asset_id_of(self, address: str) -> str | None:
        """地址对应的跨链资产编号；原生币传 NATIVE；不在清单里返回 None。"""
        key = NATIVE_ADDRESS if address == NATIVE else address.lower()
        return next((a.asset_id for a in self.assets if a.address == key), None)


def parse_chain_profile(data: dict, *, source: str) -> ChainProfile:
    """把一份链画像的内容解析成 `ChainProfile`。

    @param source 文件名，用于错误信息
    @raises ConfigError 内容不符合 schema，或链没有在 alpha_core 登记规格
    """
    try:
        f = ChainProfileFile.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{source}：{exc}") from exc
    if f.chain not in CHAIN_SPECS:
        raise ConfigError(f"{source}：链 {f.chain} 没有在 alpha_core.types.CHAIN_SPECS 登记规格")
    return ChainProfile(
        chain=f.chain,
        vm=f.vm,
        native_symbol=f.native.symbol,
        native_decimals=f.native.decimals,
        wrapped_native=f.wrapped_native,
        rules=ChainRules(f.gas_model, frozenset(f.system_tx_rules)),
        create2_variant=f.create2_variant,
        provider_slugs=dict(f.provider_slugs),
        assets=tuple(f.assets),
    )


def load_chain_profiles(directory: Path | None = None) -> dict[Chain, ChainProfile]:
    """加载目录下全部链画像；文件名必须等于链名。

    @param directory 默认为包内的 `chains/`
    @raises ConfigError 任何一份链画像有误
    """
    directory = directory or package_dir("chains")
    out: dict[Chain, ChainProfile] = {}
    for path in sorted(directory.glob("*.yaml")):
        profile = parse_chain_profile(read_yaml(path), source=path.name)
        if path.stem != profile.chain.value:
            raise ConfigError(f"{path.name}：文件名必须等于链名 {profile.chain.value}")
        out[profile.chain] = profile
    return out


@cache
def chain_profiles() -> Mapping[Chain, ChainProfile]:
    """包内链画像（加载一次后缓存）。"""
    return load_chain_profiles()
