"""钱包分析 M2：chain_txs 补完整调用数据和 L2 字段

- input_data：完整调用数据。部分解码规则要读调用参数（例如 `unwrapWETH9` 的收款人、
  `repayBorrowBehalf` 的借款人），只存方法选择器无法做到"从库里重新解码，零 RPC"；
- tx_type、mint_raw：OP Stack 存款交易（类型 0x7e）在 L2 上凭空铸造原生币，不产生日志；
- l1_fee：OP Stack 等 L2 回执里的 L1 数据费，钱包实际支付的 gas 要加上它。

全部可空：已有的 BSC 数据没有这些值，按需回填，不强制。设计见
docs/wallet-analyzer-M2-协议解码核心实施规划.md 5.12。

Revision ID: 0007_chain_tx_fields
Revises: 0006_chain_data_cache
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_chain_tx_fields"
down_revision = "0006_chain_data_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chain_txs", sa.Column("input_data", sa.Text(), nullable=True))
    op.add_column("chain_txs", sa.Column("tx_type", sa.SmallInteger(), nullable=True))
    op.add_column("chain_txs", sa.Column("mint_raw", sa.Numeric(78, 0), nullable=True))
    op.add_column("chain_txs", sa.Column("l1_fee", sa.Numeric(78, 0), nullable=True))


def downgrade() -> None:
    op.drop_column("chain_txs", "l1_fee")
    op.drop_column("chain_txs", "mint_raw")
    op.drop_column("chain_txs", "tx_type")
    op.drop_column("chain_txs", "input_data")
