"""钱包分析 M1：通用链数据缓存与运维表

新增 block_times / chain_txs / chain_logs / abi_cache / price_points / external_call_ledger /
chain_state_cache 七张表，并给 tokens 加元数据列。设计见
docs/wallet-analyzer-M1-数据基建实施规划.md 第 5 节。

新表的文本列一律用 VARCHAR（不用 CHAR：CHAR(N) 读出来会带尾部空格，在 Python 里比较或
序列化时出错）；地址和哈希统一小写、带 0x 前缀；tokens 原有的 CHAR 列不动，避免影响现有调用方。

Revision ID: 0006_chain_data_cache
Revises: 0005_pool_ohlcv
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_chain_data_cache"
down_revision = "0005_pool_ohlcv"
branch_labels = None
depends_on = None

_TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "block_times",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("block_number", sa.BigInteger(), primary_key=True),
        sa.Column("block_time", _TS, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
    )

    op.add_column("tokens", sa.Column("symbol", sa.String(64), nullable=True))
    op.add_column("tokens", sa.Column("name", sa.String(128), nullable=True))
    op.add_column("tokens", sa.Column("standard", sa.String(8), nullable=True))
    op.add_column("tokens", sa.Column("source", sa.String(16), nullable=False, server_default="rpc"))
    op.add_column("tokens", sa.Column("risk_flag", sa.String(16), nullable=False, server_default="normal"))
    op.add_column("tokens", sa.Column("updated_at", _TS, nullable=True))

    op.create_table(
        "chain_txs",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("tx_hash", sa.String(66), primary_key=True),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("tx_index", sa.Integer(), nullable=True),
        sa.Column("from_address", sa.String(42), nullable=False),
        sa.Column("to_address", sa.String(42), nullable=True),
        sa.Column("value_raw", sa.Numeric(78, 0), nullable=False, server_default="0"),
        sa.Column("method_selector", sa.String(10), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=True),
        sa.Column("gas_used", sa.BigInteger(), nullable=True),
        sa.Column("effective_gas_price", sa.Numeric(78, 0), nullable=True),
        sa.Column("contract_address", sa.String(42), nullable=True),
        sa.Column("receipt_fetched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("first_seen_at", _TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chain_txs_chain_block", "chain_txs", ["chain", "block_number"])
    op.create_index(
        "ix_chain_txs_missing_receipt",
        "chain_txs",
        ["chain"],
        postgresql_where=sa.text("receipt_fetched = false"),
    )

    op.create_table(
        "chain_logs",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("tx_hash", sa.String(66), primary_key=True),
        sa.Column("log_index", sa.Integer(), primary_key=True),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("topic0", sa.String(66), nullable=True),
        sa.Column("topic1", sa.String(66), nullable=True),
        sa.Column("topic2", sa.String(66), nullable=True),
        sa.Column("topic3", sa.String(66), nullable=True),
        sa.Column("data", sa.Text(), nullable=False),
    )
    op.create_index("ix_chain_logs_address_topic0", "chain_logs", ["chain", "address", "topic0"])
    op.create_index("ix_chain_logs_topic0", "chain_logs", ["chain", "topic0"])

    op.create_table(
        "abi_cache",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("key_type", sa.String(16), primary_key=True),
        sa.Column("key", sa.String(66), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column("name", sa.String(256), nullable=True),
        sa.Column("abi", JSONB(), nullable=True),
        sa.Column("fetched_at", _TS, nullable=False),
        sa.Column("retry_after", _TS, nullable=True),
    )

    op.create_table(
        "price_points",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("token_address", sa.String(42), primary_key=True),
        sa.Column("granularity", sa.String(8), primary_key=True),
        sa.Column("bucket_start", _TS, primary_key=True),
        sa.Column("price_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(128), nullable=True),
        sa.Column("confidence", sa.String(8), nullable=False),
        sa.Column("reference_price_usd", sa.Numeric(38, 18), nullable=True),
        sa.Column("fetched_at", _TS, nullable=False),
    )

    op.create_table(
        "external_call_ledger",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("app", sa.String(32), primary_key=True),
        sa.Column("provider", sa.String(32), primary_key=True),
        sa.Column("method", sa.String(64), primary_key=True),
        sa.Column("job_ref", sa.String(64), primary_key=True, server_default=""),
        sa.Column("status", sa.String(16), primary_key=True),
        sa.Column("call_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("est_cu", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "chain_state_cache",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("key", sa.String(256), primary_key=True),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=True),
        sa.Column("fetched_at", _TS, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("chain_state_cache")
    op.drop_table("external_call_ledger")
    op.drop_table("price_points")
    op.drop_table("abi_cache")
    op.drop_index("ix_chain_logs_topic0", table_name="chain_logs")
    op.drop_index("ix_chain_logs_address_topic0", table_name="chain_logs")
    op.drop_table("chain_logs")
    op.drop_index("ix_chain_txs_missing_receipt", table_name="chain_txs")
    op.drop_index("ix_chain_txs_chain_block", table_name="chain_txs")
    op.drop_table("chain_txs")
    for col in ("updated_at", "risk_flag", "source", "standard", "name", "symbol"):
        op.drop_column("tokens", col)
    op.drop_table("block_times")
