"""NonfungiblePositionManager（NPM）调用数据的解析（纯函数）。

解码需要知道原生币、token 最终转给了谁：`collect` 的收款方写成 NPM 自己时，NPM 随后在同一个
multicall 里用 `unwrapWETH9(amountMin, recipient)` 或 `sweepToken(token, amountMin, recipient)`
把钱转出去，收款方只在调用参数里，不在任何日志里。PancakeSwap 和 Uniswap 的 NPM 这几个方法签名相同。
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import decode as abi_decode
from eth_utils import keccak


def _selector(signature: str) -> str:
    return keccak(text=signature).hex()[:8]


MULTICALL = _selector("multicall(bytes[])")
MULTICALL_DEADLINE = _selector("multicall(uint256,bytes[])")
UNWRAP_WETH9 = _selector("unwrapWETH9(uint256,address)")
SWEEP_TOKEN = _selector("sweepToken(address,uint256,address)")
REFUND_ETH = _selector("refundETH()")


@dataclass(frozen=True)
class NpmCalls:
    """一笔交易里对 NPM 的调用（multicall 展开后）。"""

    selectors: tuple[str, ...]  # 按调用顺序的 4 字节选择器（十六进制，不带 0x）
    unwrap_recipients: tuple[str, ...]  # unwrapWETH9 的收款方
    sweeps: tuple[tuple[str, str], ...]  # sweepToken 的 (token, 收款方)

    @property
    def refunds_eth(self) -> bool:
        return REFUND_ETH in self.selectors


def parse_npm_calls(calldata: str) -> NpmCalls:
    """展开 NPM 的调用数据；解析失败的内层调用跳过（只影响收款方识别，由调用方告警）。"""
    raw = calldata.lower().removeprefix("0x")
    selector, body = raw[:8], bytes.fromhex(raw[8:])
    if selector == MULTICALL:
        inner = [c.hex() for c in abi_decode(["bytes[]"], body)[0]]
    elif selector == MULTICALL_DEADLINE:
        inner = [c.hex() for c in abi_decode(["uint256", "bytes[]"], body)[1]]
    else:
        inner = [raw]
    selectors, unwraps, sweeps = [], [], []
    for call in inner:
        sel, args = call[:8], bytes.fromhex(call[8:])
        selectors.append(sel)
        try:
            if sel == UNWRAP_WETH9:
                unwraps.append(abi_decode(["uint256", "address"], args)[1].lower())
            elif sel == SWEEP_TOKEN:
                token, _min, recipient = abi_decode(["address", "uint256", "address"], args)
                sweeps.append((token.lower(), recipient.lower()))
        except Exception:  # noqa: BLE001, S112 - 参数不符合签名：跳过这一个内层调用，收款方识别不到由调用方告警
            continue
    return NpmCalls(tuple(selectors), tuple(unwraps), tuple(sweeps))
