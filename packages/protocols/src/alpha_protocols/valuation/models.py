"""估值的数据模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from ..decoding.models import PositionRef


@dataclass(frozen=True)
class StateRead:
    """一次只读合约调用的声明（EVM 的 eth_call）。由调用方批量执行，同一轮在同一个区块上。"""

    key: str  # 结果的键，估值器用它取回结果；同一次估值里必须唯一
    to: str  # 目标合约
    data: str  # 调用数据，0x 开头


# 读取结果：键 → 返回数据；调用失败（revert、节点错误）为 None，估值器自行决定怎么处理
ReadResults = Mapping[str, bytes | None]


class Component(StrEnum):
    """底层资产数量的组成部分。"""

    PRINCIPAL = "principal"  # 本金（仓位里的资产本身）
    FEE = "fee"  # 未领取的手续费（LP）
    REWARD = "reward"  # 未领取的协议奖励
    DEBT = "debt"  # 负债（数量为正，符号由 sign 表示）


@dataclass(frozen=True)
class UnderlyingAmount:
    asset: str  # 底层资产地址；原生币为 NATIVE
    amount_raw: int  # 原始整数数量，≥0
    component: Component
    sign: int = 1  # +1 资产，−1 负债
    lower_bound: bool = False  # 只是下限（例如只读到了已结算的奖励，之后新产生的没算进去）


@dataclass(frozen=True)
class ValuationRequest:
    """一次估值请求。

    `amount_raw` 为 None 时估值器按持有人当前持有的数量估值（例如读 NFT 仓位本身、读份额余额）；
    嵌套解包时由调度器给出具体数量（例如"金库里的 100 个 LP 值多少底层资产"）。
    """

    position: PositionRef
    amount_raw: int | None = None


@dataclass(frozen=True)
class Valuation:
    """一个持仓的估值结果（已递归解包到不能再解的资产）。"""

    request: ValuationRequest
    amounts: tuple[UnderlyingAmount, ...]
    extra: Mapping[str, Any] = field(default_factory=dict)  # 估值器给出的附加信息，例如是否在区间内、健康度
    error: str | None = None  # 估值失败的原因（读取失败、状态异常）；失败时 amounts 为空


class PositionValuer(Protocol):
    """一个实例的估值器，由家族按实例部署构造（`ProtocolFamily.valuer`）。"""

    instance_key: str

    def plan(self, request: ValuationRequest, reads: ReadResults) -> list[StateRead]:
        """根据已有的读取结果，声明还需要读什么；不再需要时返回空列表。第一轮 `reads` 为空。"""

    def unwrap(self, request: ValuationRequest, reads: ReadResults) -> Valuation:
        """全部读取完成后，把持仓解包成底层资产数量（纯函数）。"""
