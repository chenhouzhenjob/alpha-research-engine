"""Uniswap V3 系池子的通用机制（规划 5.8，步骤 6 从旧 pancakeswap_v3 插件迁入）。

这里只放各分叉共有的知识，所有部署相关的值（工厂地址、费率档位、分叉变体）都由调用方传入，
不写死任何链或地址。旧插件 `plugins/pancakeswap_v3.py` 绑定 PancakeSwap（BSC）的常量后委托到这里。

分叉之间的差异：
- **Swap 事件**：Uniswap 原版 7 个字段；PancakeSwap 多两个尾部的协议费字段（protocolFeesToken0/1）。
  两者 topic0 不同，解码时按 topic0 自动识别，不需要配置；
- **slot0 里 feeProtocol 的打包方式**：Uniswap 是一个 uint8（低 4 位 token0、高 4 位 token1，值为 1/N 的分母）；
  PancakeSwap 是一个 uint32（低 16 位 token0、高 16 位 token1，单位 1/10000）。读合约状态无法从返回值
  判断是哪种，必须由调用方用 `Variant` 声明。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from alpha_chains.base import ChainAdapter
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak


def _topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


class Variant(StrEnum):
    """V3 分叉变体。"""

    UNISWAP = "uniswap"  # Uniswap V3 原版
    PANCAKE = "pancake"  # PancakeSwap V3：Swap 多两个协议费字段，feeProtocol 用 16 位打包


POOL_CREATED_SIGNATURE = "PoolCreated(address,address,uint24,int24,address)"
POOL_CREATED = _topic(POOL_CREATED_SIGNATURE)
SWAP_SIGNATURES: dict[Variant, str] = {
    Variant.UNISWAP: "Swap(address,address,int256,int256,uint160,uint128,int24)",
    Variant.PANCAKE: "Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)",
}
SWAP_TOPICS: dict[str, Variant] = {_topic(sig): v for v, sig in SWAP_SIGNATURES.items()}
_SWAP_DATA_TYPES: dict[Variant, list[str]] = {
    Variant.UNISWAP: ["int256", "int256", "uint160", "uint128", "int24"],
    Variant.PANCAKE: ["int256", "int256", "uint160", "uint128", "int24", "uint128", "uint128"],
}

_GET_POOL = bytes.fromhex(keccak(text="getPool(address,address,uint24)").hex()[:8])
_SLOT0 = bytes.fromhex(keccak(text="slot0()").hex()[:8])


def _hex(value: str) -> str:
    """日志字段统一成带 0x 的小写（旧的 `LogEntry` 的 topic 不带 0x）。"""
    v = value.lower()
    return v if v.startswith("0x") else "0x" + v


def _addr(topic: str) -> str:
    return "0x" + _hex(topic)[-40:]


@dataclass(frozen=True)
class PoolCreated:
    """Factory 的 PoolCreated 事件。"""

    token0: str
    token1: str
    fee: int  # 费率，单位 1e-6
    tick_spacing: int
    pool: str  # 小写地址


def decode_pool_created(topics: Sequence[str], data: str) -> PoolCreated:
    """解码 `PoolCreated(token0 indexed, token1 indexed, fee indexed, tickSpacing, pool)`。"""
    _t0, token0, token1, fee = topics
    tick_spacing, pool = abi_decode(["int24", "address"], bytes.fromhex(_hex(data)[2:]))
    return PoolCreated(_addr(token0), _addr(token1), int(_hex(fee), 16), tick_spacing, pool.lower())


@dataclass(frozen=True)
class Swap:
    """池子的一次 Swap。数量以池子视角记：正数为池子收到，负数为池子付出。"""

    variant: Variant
    sender: str
    recipient: str
    amount0: int
    amount1: int
    sqrt_price_x96: int  # 成交后价格
    liquidity: int  # 成交后的活跃流动性
    tick: int  # 成交后的 tick
    protocol_fees: tuple[int, int] | None  # PancakeSwap 变体的协议费（token0, token1）；原版为 None


def swap_variant(topic0: str) -> Variant | None:
    """按 topic0 识别 Swap 事件的变体；不是 V3 Swap 返回 None。"""
    return SWAP_TOPICS.get(_hex(topic0))


def decode_swap(topics: Sequence[str], data: str) -> Swap:
    """解码 Swap 事件，变体按 topic0 自动识别。

    @raises ValueError topic0 不是任何已知变体的 Swap
    """
    variant = swap_variant(topics[0])
    if variant is None:
        raise ValueError(f"不是 V3 Swap 事件：{topics[0]}")
    values = abi_decode(_SWAP_DATA_TYPES[variant], bytes.fromhex(_hex(data)[2:]))
    amount0, amount1, sqrt_price, liquidity, tick = values[:5]
    fees = (values[5], values[6]) if variant is Variant.PANCAKE else None
    return Swap(variant, _addr(topics[1]), _addr(topics[2]), amount0, amount1, sqrt_price, liquidity, tick, fees)


def get_pool(
    adapter: ChainAdapter, factory: str, token_a: str, token_b: str, fee: int, fee_tick_spacing: Mapping[int, int]
) -> str | None:
    """调用 `Factory.getPool` 查询 token 对 + 费率的池子地址；不存在返回 None。

    @param fee_tick_spacing 该部署支持的费率档位（费率 → tickSpacing）；各分叉档位不同
    @raises ValueError 费率不在档位里
    """
    if fee not in fee_tick_spacing:
        raise ValueError(f"未知费率档位: {fee}")
    token0, token1 = sorted([token_a.lower(), token_b.lower()])
    data = _GET_POOL + abi_encode(["address", "address", "uint24"], [token0, token1, fee])
    result = adapter.call(to=factory, data="0x" + data.hex())
    pool = int.from_bytes(result[-20:], "big")
    return None if pool == 0 else "0x" + result[-20:].hex()


@dataclass(frozen=True)
class Slot0:
    """`slot0()` 里用得到的字段。"""

    sqrt_price_x96: int
    tick: int
    fee_protocol0: int  # token0 的协议费参数；含义随变体不同，见模块说明
    fee_protocol1: int


def decode_slot0(result: bytes, variant: Variant) -> Slot0:
    """解 `slot0()` 的返回值：word0 sqrtPriceX96、word1 tick、word5 feeProtocol（打包方式随变体）。"""
    sqrt_price = int.from_bytes(result[0:32], "big")
    tick = int.from_bytes(result[32:64], "big", signed=True)
    packed = int.from_bytes(result[5 * 32 : 6 * 32], "big")
    if variant is Variant.PANCAKE:
        return Slot0(sqrt_price, tick, packed & 0xFFFF, (packed >> 16) & 0xFFFF)
    return Slot0(sqrt_price, tick, packed & 0x0F, (packed >> 4) & 0x0F)


def read_slot0(adapter: ChainAdapter, pool: str, variant: Variant) -> Slot0:
    """读取池子当前的 slot0。"""
    return decode_slot0(adapter.call(to=pool, data="0x" + _SLOT0.hex()), variant)
