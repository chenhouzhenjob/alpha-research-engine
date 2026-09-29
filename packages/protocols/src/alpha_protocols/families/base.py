"""协议家族接口（规划 5.7）。

一个家族定义一类协议的共同语义：有哪些合约角色、实例配置里有哪些参数、特征事件签名，
以及（后续步骤补上的）怎么解码、怎么发现合约、怎么估值。具体协议（PancakeSwap V2、Uniswap V2……）
是家族的实例，只写配置不写代码。

接口分步补齐：配置相关部分、解码器（`decoder`）、估值器（`valuer`）已有；合约发现在步骤 12
随第一个实现一起加入，不提前定义没有实现的接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from ..config.chain_profiles import ChainProfile
    from ..config.instances import Deployment
    from ..decoding.evm.dispatch import FamilyDecoder
    from ..valuation.models import PositionValuer


class FamilyOptions(BaseModel):
    """家族实例配置项的基类：未知字段报错、不可变。各家族继承它声明自己的配置项。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProtocolFamily(ABC):
    """协议家族的基类。子类以类属性声明家族的静态信息。"""

    key: ClassVar[str]  # 家族键，例如 "uniswap_v2_like"；实例配置的 family 字段引用它
    version: ClassVar[int]  # 解码器版本；解码规则变化时加 1，写进事件的 decoder_version
    roles: ClassVar[frozenset[str]]  # 实例配置里允许的合约角色，例如 {"factory", "router"}
    options_model: ClassVar[type[FamilyOptions]] = FamilyOptions  # 实例配置项的 schema
    signature_topics: ClassVar[frozenset[str]] = frozenset()  # 特征事件的 topic0，第二阶段签名匹配识别分叉用

    @classmethod
    def decoder_version(cls) -> str:
        return f"{cls.key}@{cls.version}"

    @classmethod
    @abstractmethod
    def decoder(cls, deployment: Deployment) -> FamilyDecoder:
        """为实例在一条链上的部署构造解码器；合约地址、配置项都从 `deployment` 取，不写死在代码里。"""

    @classmethod
    def valuer(cls, deployment: Deployment, profile: ChainProfile) -> PositionValuer | None:
        """为部署构造估值器；家族没有可估值的持仓（例如包装原生币、聚合器）时返回 None。

        @param profile 部署所在链的链画像（CREATE2 变体等链级差异从这里取）
        """
        return None
