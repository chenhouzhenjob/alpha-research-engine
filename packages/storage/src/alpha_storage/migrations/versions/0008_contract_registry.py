"""钱包分析 M2：合约识别结果表 contract_registry

每个地址一行，永久缓存识别结果（实例配置的角色、注册表发现、CREATE2 校验、字节码判断 EOA；
第二阶段起还有签名匹配、LLM 判断、人工确认）。设计见 docs/wallet-analyzer-M2-协议解码核心实施规划.md 第 6 节。

Revision ID: 0008_contract_registry
Revises: 0007_chain_tx_fields
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_contract_registry"
down_revision = "0007_chain_tx_fields"
branch_labels = None
depends_on = None

_TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "contract_registry",
        sa.Column("chain", sa.String(16), primary_key=True),
        sa.Column("address", sa.String(42), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("family", sa.String(32), nullable=True),
        sa.Column("instance_key", sa.String(64), nullable=True),
        sa.Column("code_hash", sa.String(66), nullable=True),
        sa.Column("implementation_address", sa.String(42), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("review_status", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("evidence", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("identified_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_contract_registry_family", "contract_registry", ["chain", "family", "instance_key"])
    op.create_index(
        "ix_contract_registry_pending",
        "contract_registry",
        ["chain"],
        postgresql_where=sa.text("review_status = 'pending_review'"),
    )


def downgrade() -> None:
    op.drop_index("ix_contract_registry_pending", table_name="contract_registry")
    op.drop_index("ix_contract_registry_family", table_name="contract_registry")
    op.drop_table("contract_registry")
