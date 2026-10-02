"""新增 tokens 表：缓存 ERC20 decimals

`decimals()` 是 ERC20 标准里的不可变值，一经查到永久有效。之前每次 `aggregate_candles` 调用
都现场 `eth_call` 重新查（NodeReal 按 20 CU/次计费，真实跑过才发现这是纯浪费，见实施记录）。
`alpha_core.models.Token` 这个领域对象在 1a 阶段就写好了、标注"应当永久缓存"，但一直没有
配套的存储层——这张表补上这个缺口。

Revision ID: 0004_tokens
Revises: 0003_block_time
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_tokens"
down_revision = "0003_block_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tokens",
        sa.Column("chain", sa.CHAR(10), primary_key=True),
        sa.Column("token_address", sa.CHAR(42), primary_key=True),
        sa.Column("decimals", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tokens")
