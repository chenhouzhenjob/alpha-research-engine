"""链上数据值对象：回执、日志、交易。

放在 alpha_core 而不是 alpha_chains：链适配器产出它们，存储层（alpha_storage）要落库它们，
而存储层只能依赖 core。`alpha_chains.base` 会重新导出这些类型，调用方从哪边导入都可以。
"""

from __future__ import annotations

from dataclasses import dataclass

# 和 `alpha_chains.base.LogEntry`（历史遗留）不同，这些对象里的地址、哈希、topic、data
# 一律是**小写、带 0x 前缀**的十六进制字符串。`LogEntry` 的 topics/transaction_hash 不带 0x
# （由 web3 的 HexBytes.hex() 生成），下游 swap_events 依赖这个格式，所以不改它，另起新类型。


@dataclass(frozen=True)
class RawLog:
    """回执里的一条日志。"""

    address: str  # 发出日志的合约地址
    topics: list[str]  # topic0..3
    data: str  # 非 indexed 参数的 ABI 编码
    log_index: int  # 在整个区块内的日志序号
    block_number: int
    tx_hash: str


@dataclass(frozen=True)
class TxReceipt:
    """一笔交易的回执。BSC 回执不带出块时间，需要时另查 `get_block_timestamps`。"""

    tx_hash: str
    block_number: int
    tx_index: int
    from_address: str
    to_address: str | None  # 创建合约的交易为 None
    status: int | None  # 1 成功，0 失败；极老的交易可能没有这个字段，为 None
    gas_used: int
    effective_gas_price: int | None  # wei；部分节点对老交易不返回，为 None
    contract_address: str | None  # 创建合约的交易所创建的地址；其他交易为 None
    logs: list[RawLog]


@dataclass(frozen=True)
class TxInfo:
    """一笔交易本身（`eth_getTransactionByHash`）。"""

    tx_hash: str
    block_number: int | None  # 尚未打包的交易为 None
    tx_index: int | None
    from_address: str
    to_address: str | None  # 创建合约的交易为 None
    value: int  # 原生币数量（wei）
    input: str  # 完整调用数据，0x 开头；普通转账为 "0x"
    nonce: int
    gas_price: int | None  # wei

    @property
    def method_selector(self) -> str | None:
        """调用数据的前 4 字节；没有调用数据时为 None。"""
        return self.input[:10] if len(self.input) >= 10 else None
