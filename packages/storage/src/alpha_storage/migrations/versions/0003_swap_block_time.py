"""swap_events 加 block_time 列

`aggregate_candles` 之前每次运行都要对 swap_events 里每个不同区块调一次
`get_block_timestamp`（RPC 调用），没有跨进程缓存——实测 subscribe_swaps 常驻跑起来之后
swap_events 会持续增长，几天内单次聚合就会退化到几十分钟甚至几小时，无法支撑定时任务
（见实施记录）。这一列让订阅进程落库时就把出块时间存下来（WebSocket 订阅 payload 里免费带了
blockTimestamp 字段，见 alpha_chains.evm_websocket 的模块文档），聚合阶段直接读，不用重查。

先加成可空列，不在这次迁移里做数据回填（回填需要现场调 RPC，混进 schema 迁移不合适）——
已有的历史行留 NULL，用单独脚本回填一次即可，见部署记录。

Revision ID: 0003_block_time
Revises: 0002_realtime_data
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_block_time"
down_revision = "0002_realtime_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("swap_events", sa.Column("block_time", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("swap_events", "block_time")
