"""实时研究驱动决策系统 · 阶段 0：pool_candidates 加 asset_class + 新增 instruments/swap_events/ohlcv

`asset_class` 决定 RWA 专属特征/模型（参考价、TrackingError、GapJump）要不要对某个候选池跑。
新增三张表支撑 WebSocket 实时数据链路：`instruments`（标的目录，字段命名对齐
`alpha-research-engine` 的 envelope/instrument_id 约定，方便以后互通，不是这次顺手抄的样式）、
`swap_events`（逐笔 Swap 事件，最细粒度原始数据）、`ohlcv`（K 线，字段命名同样对齐 envelope 约定，
但物理存储仍是 Postgres，不是对方用的 Parquet——WebSocket 实时写入 + HTTP 接口查最新数据这个
访问模式更适合 Postgres）。详见 research/docs/live-signal-system-设计方案.md。

Revision ID: 0002_realtime_data
Revises: 0001_initial
Create Date: 2026-08-16

注意：alembic_version.version_num 默认是 VARCHAR(32)，revision id 不能取太长的描述性名字
（第一次写成 "0002_asset_class_and_realtime_tables"，37 字符，实测直接把迁移自身写挂了）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_realtime_data"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pool_candidates",
        sa.Column("asset_class", sa.CHAR(15), nullable=False, server_default="crypto_native"),
    )

    op.create_table(
        "instruments",
        # TEXT 而非 CHAR(N)：instrument_id 是变长自然键，读回 Python 后会被原始比较/JSON
        # 序列化——CHAR(N) 读回来带尾随空格 padding 且不会被自动 strip（已用真实查询验证），
        # 只有 SQL 层比较才会被 Postgres 忽略尾随空格，脱离 SQL 上下文会静默出错。
        sa.Column("instrument_id", sa.Text(), primary_key=True),
        sa.Column("venue", sa.CHAR(30), nullable=False),
        sa.Column("market_type", sa.CHAR(20), nullable=False),
        sa.Column("base", sa.CHAR(42), nullable=False),
        sa.Column("quote", sa.CHAR(42), nullable=False),
        sa.Column("settle", sa.CHAR(42), nullable=True),
        sa.Column("symbol_raw", sa.CHAR(42), nullable=False),
        sa.Column("chain", sa.CHAR(10), nullable=False),
        sa.Column("listed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delisted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("meta_json", JSONB(), nullable=True),
    )

    op.create_table(
        "swap_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("chain", sa.CHAR(10), nullable=False),
        sa.Column("pool_address", sa.CHAR(42), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("tx_hash", sa.CHAR(66), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("sender", sa.CHAR(42), nullable=False),
        sa.Column("recipient", sa.CHAR(42), nullable=False),
        sa.Column("amount0", sa.Numeric(78, 0), nullable=False),
        sa.Column("amount1", sa.Numeric(78, 0), nullable=False),
        sa.Column("sqrt_price_x96_after", sa.Numeric(78, 0), nullable=False),
        sa.Column("tick_after", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("chain", "tx_hash", "log_index", name="uq_swap_events_chain_tx_log"),
    )
    op.create_index(
        "ix_swap_events_chain_pool_block", "swap_events", ["chain", "pool_address", "block_number"]
    )

    op.create_table(
        "ohlcv",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("dataset", sa.CHAR(10), nullable=False, server_default="ohlcv"),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("venue", sa.CHAR(30), nullable=False),
        sa.Column("tf", sa.CHAR(4), nullable=False),
        sa.Column("ts_event", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ts_ingest", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(), nullable=False),
        sa.Column("high", sa.Numeric(), nullable=False),
        sa.Column("low", sa.Numeric(), nullable=False),
        sa.Column("close", sa.Numeric(), nullable=False),
        sa.Column("volume", sa.Numeric(), nullable=False),
        sa.Column("quote_volume", sa.Numeric(), nullable=True),
        sa.Column("trade_count", sa.Integer(), nullable=True),
        sa.UniqueConstraint("instrument_id", "tf", "ts_event", name="uq_ohlcv_instrument_tf_ts"),
    )


def downgrade() -> None:
    op.drop_table("ohlcv")
    op.drop_table("swap_events")
    op.drop_table("instruments")
    op.drop_column("pool_candidates", "asset_class")
