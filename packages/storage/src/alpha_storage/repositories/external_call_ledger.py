"""外部调用额度账本仓储：把 `InMemoryCallMeter` 的累计值叠加进 `external_call_ledger`。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from alpha_core.metering import InMemoryCallMeter
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import ExternalCallLedgerRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LedgerLine:
    """账本汇总的一行。"""

    provider: str
    method: str
    status: str
    call_count: int
    est_cu: int | None  # 有单价未知的调用时为 None


class ExternalCallLedgerRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def flush(self, meter: InMemoryCallMeter) -> int:
        """取出计量器的累计值并叠加进账本；写库失败时把累计值放回计量器，不丢数。

        @returns 写入（新增或累加）的行数
        """
        totals = meter.drain()
        if not totals:
            return 0
        try:
            for key, total in totals.items():
                stmt = insert(ExternalCallLedgerRow).values(
                    day=key.day,
                    app=key.app,
                    provider=key.provider,
                    method=key.method,
                    job_ref=key.job_ref,
                    status=key.status.value,
                    call_count=total.call_count,
                    est_cu=total.est_cu,
                )
                row = ExternalCallLedgerRow
                stmt = stmt.on_conflict_do_update(
                    index_elements=["day", "app", "provider", "method", "job_ref", "status"],
                    set_={
                        "call_count": row.call_count + stmt.excluded.call_count,
                        # 任一方为 NULL（单价未知）时结果为 NULL，表示合计不完整。
                        "est_cu": row.est_cu + stmt.excluded.est_cu,
                        "updated_at": func.now(),
                    },
                )
                self._session.execute(stmt)
            self._session.flush()
        except Exception:
            meter.restore(totals)
            logger.exception("额度账本写入失败，累计值已放回计量器")
            raise
        return len(totals)

    def summarize(self, *, since: date, app: str | None = None, job_ref: str | None = None) -> list[LedgerLine]:
        """按供应商、方法、状态汇总某日起的调用次数和 CU。"""
        row = ExternalCallLedgerRow
        stmt = (
            select(
                row.provider,
                row.method,
                row.status,
                func.sum(row.call_count),
                func.sum(row.est_cu),
                func.bool_or(row.est_cu.is_(None)),  # 任一行 CU 未知，合计就不完整
            )
            .where(row.day >= since)
            .group_by(row.provider, row.method, row.status)
            .order_by(row.provider, row.method, row.status)
        )
        if app is not None:
            stmt = stmt.where(row.app == app)
        if job_ref is not None:
            stmt = stmt.where(row.job_ref == job_ref)
        return [
            LedgerLine(provider, method, status, int(calls), None if incomplete else int(cu or 0))
            for provider, method, status, calls, cu, incomplete in self._session.execute(stmt)
        ]
