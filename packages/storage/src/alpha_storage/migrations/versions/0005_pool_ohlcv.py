"""ohlcv 表改名为 pool_ohlcv

这张表只存链上池子的 K 线——以后要接 CEX/股票的 OHLCV 会是各自独立的表
（如 `cex_ohlcv`/`stock_ohlcv`），不会混进这张表，表名直接体现这个边界，
不用等到真的接入第二种资产类型才发现名字取早了。

Revision ID: 0005_pool_ohlcv
Revises: 0004_tokens
Create Date: 2026-08-16
"""

from __future__ import annotations

from alembic import op

revision = "0005_pool_ohlcv"
down_revision = "0004_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("ohlcv", "pool_ohlcv")
    op.execute("ALTER TABLE pool_ohlcv RENAME CONSTRAINT uq_ohlcv_instrument_tf_ts TO uq_pool_ohlcv_instrument_tf_ts")


def downgrade() -> None:
    op.execute("ALTER TABLE pool_ohlcv RENAME CONSTRAINT uq_pool_ohlcv_instrument_tf_ts TO uq_ohlcv_instrument_tf_ts")
    op.rename_table("pool_ohlcv", "ohlcv")
