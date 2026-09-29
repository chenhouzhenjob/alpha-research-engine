"""识别第一层的"发现方式"声明（纯数据，规划 5.7）。

家族只声明怎么发现自己的合约，执行在 `identification/runner.py`。每种方式都是通用的，家族只填参数：
- `RegistryCall`：调用一次注册表函数拿到全部子合约（Venus 的 Comptroller.getAllMarkets()）；
- `Create2Rule`：给定一个地址，先读出盐的组成部分（V2 的 token0、token1；V3 还有 fee），再用 CREATE2 在本地
  算地址，和这个地址相等才确认（V2 交易对、V3 池子）。`known_table`（V3 的 pool_candidates）也走这条规则：
  盐的组成部分直接从表里取，同样在本地校验，不发 RPC；
- 实例配置里写明的角色地址（`static_roles`）不需要家族声明，所有家族通用。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from eth_abi import decode as abi_decode

from ..decoding.evm.rules import Create2Variant, create2_address


@dataclass(frozen=True)
class RegistryCall:
    """调用一次注册表函数，返回的每个地址都登记为本实例的 `kind`。"""

    instance_key: str
    family: str
    to: str  # 注册表合约
    data: str  # 调用数据，0x 开头
    kind: str  # 返回地址的角色，例如 market
    decode: Callable[[bytes], Sequence[str]]  # 返回数据 → 地址列表


@dataclass(frozen=True)
class Create2Rule:
    """用 CREATE2 校验一个地址是否是本实例部署的子合约。"""

    instance_key: str
    family: str
    kind: str  # 校验通过后的角色，例如 pool、pair
    selectors: tuple[str, ...]  # 要读的无参函数（0x 开头），结果依次作为盐的组成部分
    read_types: tuple[str, ...]  # 每个函数返回值的 ABI 类型
    salt: Callable[[tuple[Any, ...]], bytes]  # 盐的组成部分 → 32 字节盐
    deployer: str  # 执行 CREATE2 的合约
    init_code_hash: str
    variant: Create2Variant

    def decode(self, raws: Sequence[bytes | None]) -> tuple[Any, ...] | None:
        """把读取结果解成盐的组成部分；有任何一个读取失败或格式不对返回 None（说明不是这类合约）。"""
        if any(r is None or len(r) < 32 for r in raws):
            return None
        try:
            return tuple(abi_decode([t], r[:32])[0] for t, r in zip(self.read_types, raws, strict=True))
        except Exception:  # noqa: BLE001 - 返回值不是预期类型：不是这类合约，由调用方继续后面的识别方式
            return None

    def matches(self, address: str, values: tuple[Any, ...]) -> bool:
        return create2_address(self.variant, self.deployer, self.salt(values), self.init_code_hash) == address


@dataclass(frozen=True)
class DiscoveryPlan:
    registry_calls: tuple[RegistryCall, ...] = field(default_factory=tuple)
    create2_rules: tuple[Create2Rule, ...] = field(default_factory=tuple)
