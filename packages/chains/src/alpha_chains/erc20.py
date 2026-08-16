"""通用 ERC20 只读调用。跟具体 DEX 协议无关（任何 EVM 链上的任何 token 都适用），所以放在
`alpha_chains` 这一层，不是 `alpha_protocols`——协议插件层管的是"DEX 特有"的合约调用
（比如 PancakeSwap V3 的 `slot0`/`lmPool`），ERC20 标准调用不属于任何具体协议。
"""

from __future__ import annotations

from .base import ChainAdapter

# ERC20 `decimals()` 的函数选择器，keccak256(text="decimals()") 的前 4 字节
# （已用 Web3.keccak 计算并对照 BSC 上真实的 USDT/BTCB/QQQB 三个 token 验证过，均正确返回 18）。
_DECIMALS_SELECTOR = bytes.fromhex("313ce567")


def read_decimals(adapter: ChainAdapter, token_address: str) -> int:
    """读取 ERC20 token 的 `decimals()`。

    调用方不应该假设固定是 18——虽然本期用到的三个 token（USDT/BTCB/QQQB）实测都是 18，
    但跨链、跨 token 精度并不总是一致（如 Ethereum 上的 USDT 是 6 位），这个函数存在的意义
    就是不让调用方硬编码这个假设。
    """
    result = adapter.call(to=token_address, data="0x" + _DECIMALS_SELECTOR.hex())
    return int.from_bytes(result, "big")
