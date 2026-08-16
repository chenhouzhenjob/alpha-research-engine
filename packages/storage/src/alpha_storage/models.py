"""ORM 模型。字段定义与 research/SCHEMA.md 保持一致，改动需要同一改动内同步文档。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CHAR, TIMESTAMP, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PoolCandidateRow(Base):
    """候选池目录：Factory `PoolCreated` 全量扫描的落库结果。

    这是 pool-discovery-metrics-v1.md 第 0 节所说"重新引入全量目录"的落地——
    与 alpha-lp 的 `pools` 表是两回事：那张表只存"已确认关联到某个仓位/钱包"的池子，
    这张表存"Factory 里存在过的全部池子"，服务于池子发现打分场景，两者不合并。
    """

    __tablename__ = "pool_candidates"
    __table_args__ = (UniqueConstraint("chain", "pool_address", name="uq_pool_candidates_chain_pool"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(CHAR(10))  # 链标识，取值见 alpha_core.types.Chain
    dex_id: Mapped[str] = mapped_column(CHAR(30))  # DEX 标识，取值见 alpha_core.types.DexId
    pool_address: Mapped[str] = mapped_column(CHAR(42))  # 池子合约地址，小写
    token0_address: Mapped[str] = mapped_column(CHAR(42))
    token1_address: Mapped[str] = mapped_column(CHAR(42))
    fee_pips: Mapped[int]  # 手续费费率，单位 1e-6
    tick_spacing: Mapped[int]
    created_at_block: Mapped[int]  # PoolCreated 事件所在区块号
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))  # 懒加载补齐，见 7.4
    status: Mapped[str] = mapped_column(CHAR(10))  # discovered/qualified/rejected，见 PoolCandidateStatus
    discovered_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))  # 本系统首次发现该池子的时间
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))


class PoolMetricsHistoryRow(Base):
    """候选池逐日历史快照，波动率等指标计算的时间序列基础（1b 阶段消费）。"""

    __tablename__ = "pool_metrics_history"
    __table_args__ = (
        UniqueConstraint(
            "chain", "pool_address", "snapshot_date", name="uq_pool_metrics_history_pool_date"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(CHAR(10))
    pool_address: Mapped[str] = mapped_column(CHAR(42))
    snapshot_date: Mapped[date]  # 快照所属自然日（UTC）
    tvl_usd: Mapped[float | None]  # 当日 TVL；None 表示当天未取到（不能当 0 处理）
    volume_24h_usd: Mapped[float | None]  # 截至当日的滚动 24h 交易量
    close_price: Mapped[float | None]  # 当日收盘价（token0/token1 汇率）
    data_source: Mapped[str] = mapped_column(CHAR(20))  # 数据来源标识，如 geckoterminal
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))  # 实际抓取时间


class ChainCursorRow(Base):
    """按（链, DEX）维护的 Factory 扫描水位线，支持增量扫描而不是每次全量重扫。"""

    __tablename__ = "chain_cursors"
    __table_args__ = (UniqueConstraint("chain", "dex_id", name="uq_chain_cursors_chain_dex"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(CHAR(10))
    dex_id: Mapped[str] = mapped_column(CHAR(30))
    last_scanned_block: Mapped[int]  # 已扫描到的最后一个（已终结）区块号
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
