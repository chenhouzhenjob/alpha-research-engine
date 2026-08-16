"""lp-backtest 1a：候选池目录 + 历史快照 + 扫描水位线

这次迁移在 research 里重新引入"全量候选池目录"（pool_candidates），
是 lp-backtest-设计方案-v1.md 明确要求的池子发现场景所需（对应 pool-discovery-metrics-v1.md 第 0 节：
"本方案是全量扫描发现，推翻了 alpha-lp 0016_replace_pool_catalog_with_metrics.sql 的决定"）。
这张表和 alpha-lp 的 `pools` 表不是一回事：alpha-lp 那张表只保留"已确认关联仓位/钱包"的池子，
这里存的是"Factory 里存在过的全部候选池"，服务于发现打分，两边不合并、不共享。

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pool_candidates",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("chain", sa.CHAR(10), nullable=False),
        sa.Column("dex_id", sa.CHAR(30), nullable=False),
        sa.Column("pool_address", sa.CHAR(42), nullable=False),
        sa.Column("token0_address", sa.CHAR(42), nullable=False),
        sa.Column("token1_address", sa.CHAR(42), nullable=False),
        sa.Column("fee_pips", sa.Integer(), nullable=False),
        sa.Column("tick_spacing", sa.Integer(), nullable=False),
        sa.Column("created_at_block", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.CHAR(10), nullable=False),
        sa.Column("discovered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("chain", "pool_address", name="uq_pool_candidates_chain_pool"),
    )
    op.create_index(
        "ix_pool_candidates_chain_status", "pool_candidates", ["chain", "status"]
    )

    op.create_table(
        "pool_metrics_history",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("chain", sa.CHAR(10), nullable=False),
        sa.Column("pool_address", sa.CHAR(42), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("tvl_usd", sa.Float(), nullable=True),
        sa.Column("volume_24h_usd", sa.Float(), nullable=True),
        sa.Column("close_price", sa.Float(), nullable=True),
        sa.Column("data_source", sa.CHAR(20), nullable=False),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "chain", "pool_address", "snapshot_date", name="uq_pool_metrics_history_pool_date"
        ),
    )
    op.create_index(
        "ix_pool_metrics_history_pool_date",
        "pool_metrics_history",
        ["chain", "pool_address", "snapshot_date"],
    )

    op.create_table(
        "chain_cursors",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("chain", sa.CHAR(10), nullable=False),
        sa.Column("dex_id", sa.CHAR(30), nullable=False),
        sa.Column("last_scanned_block", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("chain", "dex_id", name="uq_chain_cursors_chain_dex"),
    )


def downgrade() -> None:
    op.drop_table("chain_cursors")
    op.drop_table("pool_metrics_history")
    op.drop_table("pool_candidates")
