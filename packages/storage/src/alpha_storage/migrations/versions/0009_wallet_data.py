"""钱包分析 M3：钱包数据表

- wallets：分析过的钱包；
- wallet_sync_ranges：已覆盖的区块区间集合（按数据层分开，不是单一水位线）；
- wallet_transfers：地址视角的转账（索引源、内部交易源的原始结果）；
- wallet_events：标准事件（派生数据，可从 chain_txs + chain_logs + wallet_transfers 整体重建）；
- wallet_tx_decodes：每笔交易的解码摘要（告警、未识别合约、解码器版本）；
- wallet_sync_jobs：同步和重新解码任务（状态机见 M3 实施规划 5.7）。

设计见 docs/wallet-analyzer-M3-分析引擎实施规划.md 第 6 节。

Revision ID: 0009_wallet_data
Revises: 0008_contract_registry
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0009_wallet_data"
down_revision = "0008_contract_registry"
branch_labels = None
depends_on = None

_TS = sa.TIMESTAMP(timezone=True)
_AMOUNT = sa.Numeric(78, 0)
# 迁移固定写死，不引用 models（迁移要反映当时的结构，不随代码变化）
_ACTIVE_STATES = "state IN ('estimating','awaiting_confirm','queued','running','rate_limited','paused')"


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("address", sa.String(42), primary_key=True),
        sa.Column("label", sa.String(128), nullable=True),
        sa.Column("entity_group", sa.String(64), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "wallet_sync_ranges",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("address", sa.String(42), primary_key=True),
        sa.Column("layer", sa.String(16), primary_key=True),
        sa.Column("from_block", sa.BigInteger(), primary_key=True),
        sa.Column("to_block", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("synced_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("to_block >= from_block", name="ck_wallet_sync_ranges_order"),
    )

    op.create_table(
        "wallet_transfers",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("wallet_address", sa.String(42), primary_key=True),
        sa.Column("tx_hash", sa.String(66), primary_key=True),
        sa.Column("transfer_key", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("token_address", sa.String(42), nullable=True),
        sa.Column("token_id", _AMOUNT, nullable=True),
        sa.Column("amount_raw", _AMOUNT, nullable=False),
        sa.Column("from_address", sa.String(42), nullable=False),
        sa.Column("to_address", sa.String(42), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("fetched_at", _TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_wallet_transfers_block", "wallet_transfers", ["chain", "wallet_address", "block_number"])

    op.create_table(
        "wallet_events",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("tx_hash", sa.String(66), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("subject_wallet", sa.String(42), primary_key=True),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("event_subtype", sa.String(24), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("asset", sa.String(42), nullable=True),
        sa.Column("amount_raw", _AMOUNT, nullable=True),
        sa.Column("token_id", _AMOUNT, nullable=True),
        sa.Column("counterparty_address", sa.String(42), nullable=True),
        sa.Column("family", sa.String(32), nullable=True),
        sa.Column("instance_key", sa.String(64), nullable=True),
        sa.Column("position_key", sa.String(160), nullable=True),
        sa.Column("coverage_tier", sa.String(2), nullable=False),
        sa.Column("confidence", sa.String(8), nullable=False),
        sa.Column("claimed_flow_ids", ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("extra", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("decoder_version", sa.String(32), nullable=False),
    )
    op.create_index("ix_wallet_events_block", "wallet_events", ["chain", "subject_wallet", "block_number"])
    op.create_index("ix_wallet_events_position", "wallet_events", ["chain", "subject_wallet", "position_key"])

    op.create_table(
        "wallet_tx_decodes",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("tx_hash", sa.String(66), primary_key=True),
        sa.Column("subject_wallet", sa.String(42), primary_key=True),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("path", sa.String(8), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("internal_available", sa.Boolean(), nullable=False),
        sa.Column("decoder_versions", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("unclaimed_flows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unknown_contracts", ARRAY(sa.String(42)), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("warnings", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("decoded_at", _TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_wallet_tx_decodes_block", "wallet_tx_decodes", ["chain", "subject_wallet", "block_number"])

    op.create_table(
        "wallet_sync_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("depth", sa.String(16), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("checkpoint", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("budget_usd", sa.Numeric(12, 4), nullable=False),
        sa.Column("estimated_usd", sa.Numeric(12, 4), nullable=True),
        sa.Column("used_usd", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("session_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", _TS, nullable=True),
    )
    op.create_index(
        "uq_wallet_sync_jobs_active",
        "wallet_sync_jobs",
        ["chain", "address"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_STATES),
    )
    op.create_index("ix_wallet_sync_jobs_wallet", "wallet_sync_jobs", ["chain", "address", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_wallet_sync_jobs_wallet", table_name="wallet_sync_jobs")
    op.drop_index("uq_wallet_sync_jobs_active", table_name="wallet_sync_jobs")
    op.drop_table("wallet_sync_jobs")
    op.drop_index("ix_wallet_tx_decodes_block", table_name="wallet_tx_decodes")
    op.drop_table("wallet_tx_decodes")
    op.drop_index("ix_wallet_events_position", table_name="wallet_events")
    op.drop_index("ix_wallet_events_block", table_name="wallet_events")
    op.drop_table("wallet_events")
    op.drop_index("ix_wallet_transfers_block", table_name="wallet_transfers")
    op.drop_table("wallet_transfers")
    op.drop_table("wallet_sync_ranges")
    op.drop_table("wallets")
