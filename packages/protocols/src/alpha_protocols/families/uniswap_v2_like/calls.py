"""Uniswap V2 系路由（Router02）调用数据的解析（纯函数）。

路由的每个方法按语义分成三类操作，并从调用参数里读出收款方 `to`：原生币由路由转给 `to`，
只在调用参数里，不在日志里。PancakeSwap 与 Uniswap 的路由方法签名相同。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from eth_utils import keccak


class Op(StrEnum):
    """路由调用的操作类别。"""

    ADD = "add"  # 添加流动性
    REMOVE = "remove"  # 移除流动性
    SWAP = "swap"  # 交换


@dataclass(frozen=True)
class RouterMethod:
    op: Op
    to_index: int  # 参数 `to` 在调用参数头部的第几个字
    eth_in: bool = False  # 用交易 value 付原生币
    eth_out: bool = False  # 收到原生币（路由解包 WETH 后转给 to）
    refunds_eth: bool = False  # 多付的原生币由路由退回（value − 实际包装的数量）


# 参数列表（多个方法共用）
_ADD = "address,address,uint256,uint256,uint256,uint256,address,uint256"
_ADD_ETH = "address,uint256,uint256,uint256,address,uint256"
_REMOVE = "address,address,uint256,uint256,uint256,address,uint256"
_REMOVE_ETH = "address,uint256,uint256,uint256,address,uint256"
_PERMIT = "bool,uint8,bytes32,bytes32"
_SWAP = "uint256,uint256,address[],address,uint256"  # (amountIn/Out, amountOutMin/InMax, path, to, deadline)
_SWAP_ETH_IN = "uint256,address[],address,uint256"  # (amountOutMin/Out, path, to, deadline)

# 方法名 → (参数列表, 语义)
_METHODS: dict[str, tuple[str, RouterMethod]] = {
    "addLiquidity": (_ADD, RouterMethod(Op.ADD, 6)),
    "addLiquidityETH": (_ADD_ETH, RouterMethod(Op.ADD, 4, eth_in=True, refunds_eth=True)),
    "removeLiquidity": (_REMOVE, RouterMethod(Op.REMOVE, 5)),
    "removeLiquidityWithPermit": (f"{_REMOVE},{_PERMIT}", RouterMethod(Op.REMOVE, 5)),
    "removeLiquidityETH": (_REMOVE_ETH, RouterMethod(Op.REMOVE, 4, eth_out=True)),
    "removeLiquidityETHWithPermit": (f"{_REMOVE_ETH},{_PERMIT}", RouterMethod(Op.REMOVE, 4, eth_out=True)),
    "removeLiquidityETHSupportingFeeOnTransferTokens": (_REMOVE_ETH, RouterMethod(Op.REMOVE, 4, eth_out=True)),
    "removeLiquidityETHWithPermitSupportingFeeOnTransferTokens": (
        f"{_REMOVE_ETH},{_PERMIT}",
        RouterMethod(Op.REMOVE, 4, eth_out=True),
    ),
    "swapExactTokensForTokens": (_SWAP, RouterMethod(Op.SWAP, 3)),
    "swapTokensForExactTokens": (_SWAP, RouterMethod(Op.SWAP, 3)),
    "swapExactETHForTokens": (_SWAP_ETH_IN, RouterMethod(Op.SWAP, 2, eth_in=True)),
    "swapTokensForExactETH": (_SWAP, RouterMethod(Op.SWAP, 3, eth_out=True)),
    "swapExactTokensForETH": (_SWAP, RouterMethod(Op.SWAP, 3, eth_out=True)),
    "swapETHForExactTokens": (_SWAP_ETH_IN, RouterMethod(Op.SWAP, 2, eth_in=True, refunds_eth=True)),
    "swapExactTokensForTokensSupportingFeeOnTransferTokens": (_SWAP, RouterMethod(Op.SWAP, 3)),
    "swapExactETHForTokensSupportingFeeOnTransferTokens": (_SWAP_ETH_IN, RouterMethod(Op.SWAP, 2, eth_in=True)),
    "swapExactTokensForETHSupportingFeeOnTransferTokens": (_SWAP, RouterMethod(Op.SWAP, 3, eth_out=True)),
}
ROUTER_METHODS: dict[str, RouterMethod] = {
    keccak(text=f"{name}({params})").hex()[:8]: method for name, (params, method) in _METHODS.items()
}


@dataclass(frozen=True)
class RouterCall:
    method: RouterMethod
    to: str  # 收款方


def parse_router_call(calldata: str) -> RouterCall | None:
    """解析路由调用；不是已知方法或参数长度不够时返回 None。"""
    raw = calldata.lower().removeprefix("0x")
    method = ROUTER_METHODS.get(raw[:8])
    if method is None:
        return None
    word = raw[8 + 64 * method.to_index : 8 + 64 * (method.to_index + 1)]
    if len(word) != 64:
        return None
    return RouterCall(method, "0x" + word[-40:])
