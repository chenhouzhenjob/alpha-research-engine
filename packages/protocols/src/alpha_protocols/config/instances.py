"""协议实例配置：一个协议一份 `instances/<instance_key>.yaml`，各链的部署写在 `deployments` 里（规划 5.10）。

同一家族的分叉只加一份 YAML、不写代码。共用的配置项写在顶层 `options`，某条链不同时在该链部署的
`options` 里覆盖；合并后用家族的 `options_model` 校验。

角色地址可以写成 `$chain.<字段>` 引用链画像，目前只支持 `$chain.wrapped_native`，
这样包装原生币这类"每条链一个"的合约不用在实例里重复写地址。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from alpha_core.types import Chain
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..families import FAMILIES
from ..families.base import FamilyOptions, ProtocolFamily
from ._common import ConfigError, check_address, package_dir, read_yaml
from .chain_profiles import ChainProfile, chain_profiles

# 角色地址里可以引用的链画像字段
CHAIN_REFS = {"$chain.wrapped_native": "wrapped_native"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeploymentFile(_Strict):
    """实例在一条链上的部署。"""

    roles: dict[str, list[str]]  # 角色 → 合约地址列表（小写）或 `$chain.<字段>` 引用
    options: dict[str, Any] = {}  # 覆盖共用配置项
    from_block: int | None = Field(default=None, ge=0)  # 部署区块；未知为 None

    @field_validator("roles")
    @classmethod
    def _addresses(cls, roles: dict[str, list[str]]) -> dict[str, list[str]]:
        for role, addresses in roles.items():
            if not addresses:
                raise ValueError(f"角色 {role} 没有地址")
            for a in addresses:
                if a not in CHAIN_REFS:
                    check_address(a)
        return roles


class InstanceFile(_Strict):
    """实例配置文件的 schema。"""

    instance_key: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")  # 全局唯一，例如 pancakeswap-v3
    family: str  # 家族键，必须已在 FAMILIES 注册
    protocol: str  # 协议名，例如 PancakeSwap
    version: str | None = None  # 协议版本，例如 v3
    options: dict[str, Any] = {}  # 各链共用的配置项
    deployments: dict[Chain, DeploymentFile]


@dataclass(frozen=True)
class Deployment:
    """实例在一条链上的部署（已解析引用、已按家族校验配置项）。"""

    chain: Chain
    instance_key: str
    family: str
    roles: Mapping[str, tuple[str, ...]]  # 角色 → 合约地址
    options: FamilyOptions  # 合并共用和本链覆盖之后的配置项
    from_block: int | None


@dataclass(frozen=True)
class ProtocolInstance:
    instance_key: str
    family: str
    protocol: str
    version: str | None
    deployments: Mapping[Chain, Deployment]


@dataclass(frozen=True)
class RoleBinding:
    """某条链上一个地址在实例配置里的角色（协议识别第一层的 static_roles 查表用）。"""

    instance_key: str
    family: str
    role: str


@dataclass(frozen=True)
class InstanceRegistry:
    """全部实例，以及按 (链, 地址) 反查角色的索引。"""

    instances: Mapping[str, ProtocolInstance]
    by_address: Mapping[tuple[Chain, str], RoleBinding]

    def deployments_on(self, chain: Chain) -> list[Deployment]:
        return [i.deployments[chain] for i in self.instances.values() if chain in i.deployments]


def parse_instance(
    data: dict,
    *,
    source: str,
    families: Mapping[str, type[ProtocolFamily]],
    profiles: Mapping[Chain, ChainProfile],
) -> ProtocolInstance:
    """解析一份实例配置。

    @raises ConfigError 格式错误、家族未注册、链没有链画像、角色不属于家族、配置项不符合家族 schema
    """
    try:
        f = InstanceFile.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{source}：{exc}") from exc
    family = families.get(f.family)
    if family is None:
        raise ConfigError(f"{source}：家族 {f.family!r} 没有注册")
    deployments: dict[Chain, Deployment] = {}
    for chain, dep in f.deployments.items():
        profile = profiles.get(chain)
        if profile is None:
            raise ConfigError(f"{source}：链 {chain} 没有链画像")
        unknown = set(dep.roles) - family.roles
        if unknown:
            raise ConfigError(
                f"{source}：{chain} 的角色 {sorted(unknown)} 不属于家族 {f.family}（允许 {sorted(family.roles)}）"
            )
        roles = {
            role: tuple(getattr(profile, CHAIN_REFS[a]) if a in CHAIN_REFS else a for a in addresses)
            for role, addresses in dep.roles.items()
        }
        try:
            options = family.options_model.model_validate({**f.options, **dep.options})
        except ValidationError as exc:
            raise ConfigError(f"{source}：{chain} 的配置项不符合家族 {f.family}：{exc}") from exc
        deployments[chain] = Deployment(chain, f.instance_key, f.family, roles, options, dep.from_block)
    return ProtocolInstance(f.instance_key, f.family, f.protocol, f.version, deployments)


def load_instances(
    directory: Path | None = None,
    *,
    families: Mapping[str, type[ProtocolFamily]] | None = None,
    profiles: Mapping[Chain, ChainProfile] | None = None,
) -> InstanceRegistry:
    """加载目录下全部实例配置，并检查跨文件的约束。

    - 文件名必须等于 instance_key，instance_key 全局唯一；
    - 同一条链上，同一个地址不能出现在两个角色里（否则识别时无法确定它属于谁）。

    @param directory 默认为包内的 `instances/`
    @raises ConfigError 任何一份配置有误，或违反跨文件约束
    """
    directory = directory or package_dir("instances")
    families = FAMILIES if families is None else families
    profiles = chain_profiles() if profiles is None else profiles
    instances: dict[str, ProtocolInstance] = {}
    by_address: dict[tuple[Chain, str], RoleBinding] = {}
    for path in sorted(directory.glob("*.yaml")):
        inst = parse_instance(read_yaml(path), source=path.name, families=families, profiles=profiles)
        if path.stem != inst.instance_key:
            raise ConfigError(f"{path.name}：文件名必须等于 instance_key {inst.instance_key}")
        if inst.instance_key in instances:
            raise ConfigError(f"{path.name}：instance_key {inst.instance_key} 重复")
        instances[inst.instance_key] = inst
        for chain, dep in inst.deployments.items():
            for role, addresses in dep.roles.items():
                for a in addresses:
                    prev = by_address.get((chain, a))
                    if prev is not None:
                        raise ConfigError(
                            f"{path.name}：{chain} 上的地址 {a} 同时是 {prev.instance_key}.{prev.role} 和 "
                            f"{inst.instance_key}.{role}"
                        )
                    by_address[(chain, a)] = RoleBinding(inst.instance_key, inst.family, role)
    return InstanceRegistry(instances, by_address)


@cache
def instance_registry() -> InstanceRegistry:
    """包内全部实例（加载一次后缓存）。"""
    return load_instances()
