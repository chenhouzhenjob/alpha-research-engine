"""同步任务仓储（`wallet_sync_jobs`）。

这里只负责持久化；状态转移是否合法由 wallet-analyzer 的任务层（M3 步骤 6）判断。
"同一钱包只有一个进行中的任务"由部分唯一索引兜底，执行期的互斥另由 advisory lock 保证（见 locks.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import WalletSyncJobRow


class JobKind(StrEnum):
    """任务类型。"""

    BACKFILL = "backfill"  # 同步：拉数据、取回执、解码、对账
    REDECODE = "redecode"  # 重新解码：只读库里的原始数据，零 RPC


class SyncDepth(StrEnum):
    """同步深度（设计文档 7.2）：越深越贵。"""

    TRANSFERS = "transfers"  # 只拉索引源和内部交易源，只消耗索引源额度
    DECODED = "decoded"  # 加上回执、识别、全部解码、完整性对账
    FULL = "full"  # 再加当前估值（持仓 + 余额）


class JobState(StrEnum):
    """任务状态（M3 实施规划 5.7）。"""

    ESTIMATING = "estimating"  # 正在预估成本
    AWAITING_CONFIRM = "awaiting_confirm"  # 预估超过预算，等待 --confirm
    QUEUED = "queued"  # 已批准，等执行器领取
    RUNNING = "running"  # 执行中（进程被杀时也停在这里，下次拿到锁时按 paused 处理）
    RATE_LIMITED = "rate_limited"  # 限速重试用尽或额度耗尽，已保存断点
    PAUSED = "paused"  # 进程中断，已保存断点
    FAILED = "failed"  # 不可恢复的错误，修复后可 resume
    DONE = "done"  # 全部阶段完成（终态）
    CANCELLED = "cancelled"  # 放弃或被新任务替代（终态）

    @classmethod
    def active(cls) -> frozenset[JobState]:
        """进行中的状态：同一钱包同时只能有一个。与 models.WALLET_JOB_ACTIVE_STATES_SQL 一致（有测试校验）。"""
        return frozenset({cls.ESTIMATING, cls.AWAITING_CONFIRM, cls.QUEUED, cls.RUNNING, cls.RATE_LIMITED, cls.PAUSED})

    @classmethod
    def terminal(cls) -> frozenset[JobState]:
        """终态：进入时写 finished_at，之后不再变化。failed 不是终态，可以 resume。"""
        return frozenset({cls.DONE, cls.CANCELLED})


class ActiveJobExistsError(Exception):
    """同一钱包已有进行中的任务。"""

    def __init__(self, chain: str, address: str, job_id: int | None) -> None:
        super().__init__(f"{chain}:{address} 已有进行中的任务 #{job_id}")
        self.job_id = job_id


@dataclass(frozen=True)
class JobRecord:
    """wallet_sync_jobs 的一行。金额单位为美元。"""

    id: int
    chain: str
    address: str
    kind: JobKind
    depth: SyncDepth
    state: JobState
    budget_usd: Decimal
    estimated_usd: Decimal | None  # 预估前为 None
    used_usd: Decimal
    error: str | None
    session_id: int | None  # M3 恒为 None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None  # 进入终态的时间
    checkpoint: dict[str, Any] = field(default_factory=dict)


_UNSET: Any = object()  # update 的哨兵：区分"不改"和"改成 None"


class WalletSyncJobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        chain: str,
        address: str,
        *,
        kind: JobKind,
        depth: SyncDepth,
        budget_usd: Decimal,
        state: JobState = JobState.ESTIMATING,
    ) -> JobRecord:
        """新建任务。

        异常：同一钱包已有进行中的任务时抛 ActiveJobExistsError（带已有任务的编号）。
        用 savepoint 包住插入，冲突时外层事务仍可继续使用。
        """
        address = address.lower()
        row = WalletSyncJobRow(
            chain=chain, address=address, kind=kind.value, depth=depth.value, state=state.value, budget_usd=budget_usd
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
        except IntegrityError as e:
            if "uq_wallet_sync_jobs_active" not in str(e.orig):
                raise
            existing = self.get_active(chain, address)
            raise ActiveJobExistsError(chain, address, existing.id if existing else None) from e
        self._session.refresh(row)
        return _record(row)

    def get(self, job_id: int) -> JobRecord | None:
        row = self._session.get(WalletSyncJobRow, job_id)
        return None if row is None else _record(row)

    def get_active(self, chain: str, address: str) -> JobRecord | None:
        """该钱包进行中的任务；没有时返回 None。"""
        j = WalletSyncJobRow
        row = self._session.scalars(
            select(j).where(
                j.chain == chain, j.address == address.lower(), j.state.in_([s.value for s in JobState.active()])
            )
        ).first()
        return None if row is None else _record(row)

    def list_for_wallet(self, chain: str, address: str, *, limit: int = 20) -> list[JobRecord]:
        """该钱包最近的任务，新的在前。"""
        j = WalletSyncJobRow
        rows = self._session.scalars(
            select(j)
            .where(j.chain == chain, j.address == address.lower())
            .order_by(j.created_at.desc(), j.id.desc())
            .limit(limit)
        )
        return [_record(r) for r in rows]

    def update(
        self,
        job_id: int,
        *,
        state: JobState | None = None,
        checkpoint: dict[str, Any] | None = None,
        estimated_usd: Decimal | None = _UNSET,
        used_usd: Decimal | None = None,
        error: str | None = _UNSET,
    ) -> JobRecord:
        """更新任务字段（只改传入的），同时刷新 updated_at；进入终态时写 finished_at。

        checkpoint 整体替换（调用方负责合并）。异常：任务不存在时抛 KeyError。
        """
        values: dict[str, Any] = {"updated_at": func.now()}
        if state is not None:
            values["state"] = state.value
            if state in JobState.terminal():
                values["finished_at"] = func.now()
        if checkpoint is not None:
            values["checkpoint"] = checkpoint
        if estimated_usd is not _UNSET:
            values["estimated_usd"] = estimated_usd
        if used_usd is not None:
            values["used_usd"] = used_usd
        if error is not _UNSET:
            values["error"] = error
        j = WalletSyncJobRow
        row = self._session.scalars(update(j).where(j.id == job_id).values(**values).returning(j)).first()
        if row is None:
            raise KeyError(f"任务 #{job_id} 不存在")
        return _record(row)


def _record(r: WalletSyncJobRow) -> JobRecord:
    return JobRecord(
        id=r.id,
        chain=r.chain,
        address=r.address,
        kind=JobKind(r.kind),
        depth=SyncDepth(r.depth),
        state=JobState(r.state),
        budget_usd=r.budget_usd,
        estimated_usd=r.estimated_usd,
        used_usd=r.used_usd,
        error=r.error,
        session_id=r.session_id,
        created_at=r.created_at,
        updated_at=r.updated_at,
        finished_at=r.finished_at,
        checkpoint=dict(r.checkpoint or {}),
    )
