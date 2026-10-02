"""通用 ERC20 只读调用。跟具体 DEX 协议无关（任何 EVM 链上的任何 token 都适用），所以放在
`alpha_chains` 这一层，不是 `alpha_protocols`——协议插件层管的是"DEX 特有"的合约调用
（比如 PancakeSwap V3 的 `slot0`/`lmPool`），ERC20 标准调用不属于任何具体协议。
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError

from .base import BatchResult, ChainAdapter
from .multicall import Call, SupportsRawCall, _collect_uints, multicall

# ERC20 `decimals()` 的函数选择器，keccak256(text="decimals()") 的前 4 字节
# （已用 Web3.keccak 计算并对照 BSC 上真实的 USDT/BTCB/QQQB 三个 token 验证过，均正确返回 18）。
_DECIMALS_SELECTOR = bytes.fromhex("313ce567")
_SYMBOL_SELECTOR = bytes.fromhex("95d89b41")  # symbol()
_NAME_SELECTOR = bytes.fromhex("06fdde03")  # name()
_BALANCE_OF_SELECTOR = bytes.fromhex("70a08231")  # balanceOf(address)


@dataclass(frozen=True)
class TokenMetadata:
    """ERC20 元数据。任何一项读不出来都为 None，调用方不能默认按 18 位精度处理。"""

    decimals: int | None  # 精度位数
    symbol: str | None  # 代币符号
    name: str | None  # 代币名称


def _decode_text(data: bytes) -> str | None:
    """解析 `symbol()`/`name()` 的返回值：标准是 ABI 编码的 string，部分老代币（如 MKR）返回 bytes32。"""
    if not data:
        return None
    try:
        (text,) = decode(["string"], data)
        return text or None
    except (DecodingError, OverflowError, ValueError):
        pass
    if len(data) == 32:
        text = data.rstrip(b"\x00").decode("utf-8", errors="ignore")
        return text or None
    return None


def read_metadata_batch(
    adapter: SupportsRawCall, tokens: list[str], *, chunk_size: int | None = None
) -> dict[str, TokenMetadata]:
    """用一批 Multicall 读取多个 token 的 decimals、symbol、name。键为小写地址。"""
    addrs = list(dict.fromkeys(t.lower() for t in tokens))
    calls = [Call(a, sel) for a in addrs for sel in (_DECIMALS_SELECTOR, _SYMBOL_SELECTOR, _NAME_SELECTOR)]
    results = multicall(adapter, calls, chunk_size=chunk_size)
    out: dict[str, TokenMetadata] = {}
    for i, addr in enumerate(addrs):
        dec, sym, name = results[3 * i : 3 * i + 3]
        decimals = int.from_bytes(dec.data[:32], "big") if dec.success and len(dec.data) >= 32 else None
        out[addr] = TokenMetadata(
            decimals=decimals if decimals is not None and decimals <= 255 else None,
            symbol=_decode_text(sym.data) if sym.success else None,
            name=_decode_text(name.data) if name.success else None,
        )
    return out


def read_balances(
    adapter: SupportsRawCall, pairs: list[tuple[str, str]], *, chunk_size: int | None = None
) -> BatchResult[tuple[str, str], int]:
    """批量读取 `(token, owner)` 余额（原始整数，未按精度换算）。键为小写的 (token, owner)。"""
    keys = list(dict.fromkeys((t.lower(), o.lower()) for t, o in pairs))
    calls = [Call(t, _BALANCE_OF_SELECTOR + encode(["address"], [o])) for t, o in keys]
    return _collect_uints(keys, multicall(adapter, calls, chunk_size=chunk_size))


def read_decimals(adapter: ChainAdapter, token_address: str) -> int:
    """读取 ERC20 token 的 `decimals()`。

    调用方不应该假设固定是 18——虽然本期用到的三个 token（USDT/BTCB/QQQB）实测都是 18，
    但跨链、跨 token 精度并不总是一致（如 Ethereum 上的 USDT 是 6 位），这个函数存在的意义
    就是不让调用方硬编码这个假设。
    """
    result = adapter.call(to=token_address, data="0x" + _DECIMALS_SELECTOR.hex())
    return int.from_bytes(result, "big")
