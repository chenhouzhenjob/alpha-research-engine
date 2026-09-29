"""解码上下文：解码一笔交易所需的、调用方提前准备好的全部外部信息（规划 5.1）。

解码是纯函数，不查库也不发请求。合约识别结果、token 元数据、ABI 都由调用方（M3 的 ingest，
M2 的测试和冒烟脚本）批量查好放进来；解码器只读这里。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import RiskFlag, TokenMeta
from .risk import classify_token


@dataclass(frozen=True)
class ContractIdentity:
    """一个合约的识别结果（`contract_registry` 的一行）。"""

    address: str
    kind: str  # 角色名（factory、router、pool、market……）或 eoa / token / unknown
    family: str | None = None  # 家族键；未知合约为 None
    instance_key: str | None = None  # 实例键；未命名分叉为 None


@dataclass(frozen=True)
class DecodeContext:
    """解码上下文。字段都是只读映射，键为小写地址或 topic0。"""

    chain: str
    tokens: Mapping[str, TokenMeta] = field(default_factory=dict)  # token 地址 → 元数据
    base_assets: Mapping[str, str] = field(
        default_factory=dict
    )  # 基础资产：地址（原生币为 NATIVE）→ symbol，来自链画像
    identities: Mapping[str, ContractIdentity] = field(default_factory=dict)  # 合约地址 → 识别结果
    contract_abis: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)  # 合约地址 → 完整 ABI
    event_signatures: Mapping[str, Sequence[str]] = field(default_factory=dict)  # topic0 → 候选事件签名（按可信度排序）

    def risk_of(self, asset: str) -> RiskFlag:
        """资产的风险标记；没有元数据的 token 视为 normal（没有证据不下结论）。"""
        meta = self.tokens.get(asset.lower())
        return classify_token(meta, self.base_assets) if meta else RiskFlag.NORMAL
