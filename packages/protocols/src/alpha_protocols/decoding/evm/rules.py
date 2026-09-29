"""链画像里声明的 EVM 链差异：gas 计费模型、系统交易规则。

两者都是封闭枚举，每个取值对应一段纯函数。新增取值意味着接入一种新的链架构，要改这里，
但每种架构只改一次；具体哪条链用哪个取值，写在链画像里，不写在代码里。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from alpha_core.chain_data import TxInfo, TxReceipt


class GasModel(StrEnum):
    """交易发起人实际支付的 gas 怎么算。"""

    STANDARD = "standard"  # gas_used × effective_gas_price。以太坊、BSC；Arbitrum 系的 L1 成本已含在 gas_used 里
    OP_STACK = "op_stack"  # 再加回执里的 l1_fee（L1 数据费）。Base、Optimism 等 OP Stack 链


class SystemTxRule(StrEnum):
    """链特有的系统交易规则。"""

    # OP Stack 存款交易（类型 0x7e）：L1 存入的 ETH 在 L2 上凭空铸造给 from（执行失败也照样铸造），
    # 不产生日志，发起人不付 gas；value 仍按普通转账从 from 转给 to（执行成功才转）
    OP_DEPOSIT = "op_deposit"


# OP Stack 存款交易的 EIP-2718 类型号
OP_DEPOSIT_TX_TYPE = 0x7E


@dataclass(frozen=True)
class ChainRules:
    """解码一条 EVM 链需要的链差异，由链画像加载器（步骤 4）构造。"""

    gas_model: GasModel = GasModel.STANDARD
    system_tx_rules: frozenset[SystemTxRule] = frozenset()


def is_op_deposit(tx: TxInfo, rules: ChainRules) -> bool:
    """这笔交易是否是链画像启用了 op_deposit 规则时的存款交易。"""
    return SystemTxRule.OP_DEPOSIT in rules.system_tx_rules and tx.tx_type == OP_DEPOSIT_TX_TYPE


def gas_paid(tx: TxInfo, receipt: TxReceipt, rules: ChainRules) -> int:
    """交易发起人实际支付的 gas（wei）。

    @raises ValueError 回执缺少计算所需的字段（例如老节点不返回 effective_gas_price 且交易里也没有 gas_price）
    """
    if is_op_deposit(tx, rules):
        return 0  # 存款交易的 gas 在 L1 上付过了
    price = receipt.effective_gas_price if receipt.effective_gas_price is not None else tx.gas_price
    if price is None:
        raise ValueError(f"交易 {tx.tx_hash} 缺少 gas 单价，无法计算 gas")
    fee = receipt.gas_used * price
    if rules.gas_model is GasModel.OP_STACK:
        # 同一条 OP Stack 链上，系统交易以外的普通交易都应该带 l1Fee；缺失时按 0 计会少算成本，所以报错
        if receipt.l1_fee is None:
            raise ValueError(f"交易 {tx.tx_hash} 的回执缺少 l1Fee，op_stack 链无法计算 gas")
        fee += receipt.l1_fee
    return fee
