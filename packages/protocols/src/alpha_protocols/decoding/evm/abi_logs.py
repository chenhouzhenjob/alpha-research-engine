"""通用 ABI 解码：把未识别合约的日志解成事件名和参数（纯函数，规划 5.4 的兜底）。

解出来的事件不带资金语义（仍是 T0），用途是给 M4 的 AI 识别和人工复核提供可读的线索。
ABI 由调用方提前用 M1 的 ABI 来源解析好传进来，本模块不发请求。两种来源：

- **完整 ABI**（按合约地址查到，例如 Sourcify）：知道哪些参数是 indexed，解码是精确的；
- **只有签名**（按 topic0 查到，例如 openchain / 4byte，形如 `Name(address,uint256)`）：不知道哪些参数
  是 indexed。按"前 N 个参数是 indexed"猜（N = topic 数 − 1，这是最常见的写法），再用 data 长度校验，
  结果标为 `signature_guess`。校验不通过就放弃，不给出可能错误的参数。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from alpha_core.chain_data import RawLog
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_abi.grammar import parse as parse_type
from eth_utils import keccak


class AbiSource(StrEnum):
    """解码用的 ABI 来源。"""

    CONTRACT_ABI = "contract_abi"  # 合约的完整 ABI，解码精确
    SIGNATURE_GUESS = "signature_guess"  # 只有事件签名，indexed 位置是按惯例猜的，已用 data 长度校验


@dataclass(frozen=True)
class DecodedLog:
    """一条解出来的日志。"""

    event: str  # 事件名
    signature: str  # 规范签名，例如 `Transfer(address,address,uint256)`
    args: Mapping[str, Any]  # 参数名 → 值；只有签名时参数名为 arg0、arg1……
    source: AbiSource


def split_types(params: str) -> list[str]:
    """把签名括号里的参数类型按顶层逗号拆开，正确处理元组类型 `(uint256,address)[]`。"""
    out, depth, cur = [], 0, []
    for ch in params:
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        depth += ch == "("
        depth -= ch == ")"
        cur.append(ch)
    if cur:
        out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


def _canonical_type(entry: Mapping[str, Any]) -> str:
    """ABI JSON 里一个参数的规范类型（元组展开成括号形式）。"""
    t = entry["type"]
    if t.startswith("tuple"):
        inner = ",".join(_canonical_type(c) for c in entry.get("components", []))
        return f"({inner}){t[len('tuple') :]}"
    return t


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, str) and value.startswith("0x"):
        return value.lower()
    return value


def _decode_topic(type_str: str, topic: str) -> Any:
    """解 indexed 参数：值类型直接解；字符串、bytes、数组、元组在 topic 里只存了哈希，原样保留哈希。"""
    abi_type = parse_type(type_str)
    if abi_type.is_dynamic or abi_type.is_array or type_str.startswith("("):
        return topic.lower()
    return _to_jsonable(abi_decode([type_str], bytes.fromhex(topic[2:]))[0])


def _decode(
    log: RawLog, names: Sequence[str], types: Sequence[str], indexed: Sequence[bool], anonymous: bool = False
) -> dict[str, Any] | None:
    topics = log.topics if anonymous else log.topics[1:]
    if sum(indexed) != len(topics):
        return None
    data_types = [t for t, i in zip(types, indexed, strict=True) if not i]
    raw = bytes.fromhex(log.data[2:])
    try:
        data_values = list(abi_decode(data_types, raw)) if data_types else []
        # 重新编码后必须和原始 data 逐字节相同：保证类型和 indexed 位置真的对得上，
        # 而不是恰好能从更长的数据里读出一段
        if abi_encode(data_types, data_values) != raw:
            return None
    except Exception:  # noqa: BLE001 - 数据和类型对不上：说明 ABI 不匹配，由调用方当作解不出来处理
        return None
    args: dict[str, Any] = {}
    topic_iter, data_iter = iter(topics), iter(data_values)
    for name, t, i in zip(names, types, indexed, strict=True):
        args[name] = _decode_topic(t, next(topic_iter)) if i else _to_jsonable(next(data_iter))
    return args


def decode_with_contract_abi(log: RawLog, abi: Sequence[Mapping[str, Any]]) -> DecodedLog | None:
    """用合约的完整 ABI 解码；ABI 里没有匹配的事件返回 None。"""
    for entry in abi:
        if entry.get("type") != "event":
            continue
        inputs = entry.get("inputs", [])
        types = [_canonical_type(i) for i in inputs]
        signature = f"{entry['name']}({','.join(types)})"
        anonymous = bool(entry.get("anonymous"))
        if not anonymous and (not log.topics or "0x" + keccak(text=signature).hex() != log.topics[0]):
            continue
        names = [i.get("name") or f"arg{k}" for k, i in enumerate(inputs)]
        args = _decode(log, names, types, [bool(i.get("indexed")) for i in inputs], anonymous)
        if args is not None:
            return DecodedLog(entry["name"], signature, args, AbiSource.CONTRACT_ABI)
    return None


def decode_with_signature(log: RawLog, signature: str) -> DecodedLog | None:
    """只有事件签名时解码：假设前 N 个参数是 indexed（N = topic 数 − 1），用 data 长度校验。"""
    if not log.topics or "(" not in signature or not signature.endswith(")"):
        return None
    if "0x" + keccak(text=signature).hex() != log.topics[0]:
        return None
    name, params = signature[: signature.index("(")], signature[signature.index("(") + 1 : -1]
    types = split_types(params)
    n_indexed = len(log.topics) - 1
    if n_indexed > len(types):
        return None
    indexed = [k < n_indexed for k in range(len(types))]
    args = _decode(log, [f"arg{k}" for k in range(len(types))], types, indexed)
    if args is None:
        return None
    return DecodedLog(name, signature, args, AbiSource.SIGNATURE_GUESS)
