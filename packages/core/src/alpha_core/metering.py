"""外部调用计量：记录每个外部数据源（RPC、HTTP API）按方法的调用次数和估算的计费单位。

为什么放在 alpha_core：`alpha_chains`（RPC）和 `alpha_datasources`（HTTP API）都要记账，
两者互不依赖，只能共同依赖 core；这里只有内存累加，不做任何 I/O——落库由
`alpha_storage.repositories.external_call_ledger.ExternalCallLedgerRepository.flush` 负责，
由调用方在任务结束或定时时调用，不在每次调用时写库（否则记账本身就成了负担）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol


class CallStatus(StrEnum):
    """一次外部调用的结果分类。"""

    OK = "ok"  # 调用成功返回
    RATE_LIMITED = "rate_limited"  # 被短时限速（每秒请求数、每秒 CU、并发），稍后即可恢复
    QUOTA_EXHAUSTED = "quota_exhausted"  # 计划额度用完（月度 CU、日请求上限），下个计费周期才恢复
    ERROR = "error"  # 其他失败（网络错误、服务端错误、返回无法解析等）


@dataclass(frozen=True)
class MeterKey:
    """账本的聚合维度，对应 `external_call_ledger` 表的主键。"""

    day: date  # UTC 日期
    app: str  # 发起调用的应用，如 wallet-analyzer / lp-backtest / oneoff
    provider: str  # 数据源供应商，如 nodereal / publicnode / sourcify
    method: str  # RPC 方法名或 HTTP 接口路径
    job_ref: str  # 关联的任务或会话，如 job:12；空串表示没有关联
    status: CallStatus


@dataclass
class MeterTotal:
    """某个聚合维度下的累计值。"""

    call_count: int = 0  # 调用次数
    est_cu: int | None = 0  # 估算的计费单位总和；只要有一次单价未知就变成 None，表示"不完整"


class CallMeter(Protocol):
    """外部调用计量端口。调用方每完成一次外部调用就 `record` 一次。"""

    def record(
        self,
        provider: str,
        method: str,
        *,
        count: int = 1,
        cu: int | None = None,
        status: CallStatus = CallStatus.OK,
    ) -> None:
        """记录一次（或一批同类）外部调用。

        @param provider 数据源供应商标识
        @param method RPC 方法名或接口路径
        @param count 调用次数，批量请求可以一次记多次
        @param cu 这 `count` 次调用的计费单位总和；单价未知时传 None
        @param status 调用结果分类
        """
        ...


class NullCallMeter:
    """什么都不记的计量器，作为默认值，保证不注入计量器时行为和原来完全一致。"""

    def record(
        self,
        provider: str,
        method: str,
        *,
        count: int = 1,
        cu: int | None = None,
        status: CallStatus = CallStatus.OK,
    ) -> None:
        return None


class InMemoryCallMeter:
    """线程安全的内存计量器：按 `MeterKey` 累加，`drain()` 取出并清空。

    `app` 和 `job_ref` 在构造时确定，也可以在任务切换时用 `set_job_ref` 修改。
    """

    def __init__(self, app: str, job_ref: str = "") -> None:
        self._app = app
        self._job_ref = job_ref
        self._lock = threading.Lock()
        self._totals: dict[MeterKey, MeterTotal] = {}

    def set_job_ref(self, job_ref: str) -> None:
        """切换之后的调用要关联的任务或会话；空串表示没有关联。"""
        with self._lock:
            self._job_ref = job_ref

    def record(
        self,
        provider: str,
        method: str,
        *,
        count: int = 1,
        cu: int | None = None,
        status: CallStatus = CallStatus.OK,
    ) -> None:
        key = MeterKey(
            day=datetime.now(UTC).date(),
            app=self._app,
            provider=provider,
            method=method,
            job_ref=self._job_ref,
            status=status,
        )
        with self._lock:
            total = self._totals.setdefault(key, MeterTotal())
            total.call_count += count
            # 单价未知的调用混进来后，这一格的 CU 合计就不再可信，用 None 显式表示不完整。
            total.est_cu = None if cu is None or total.est_cu is None else total.est_cu + cu

    def snapshot(self) -> dict[MeterKey, MeterTotal]:
        """返回当前累计值的副本，不清空。"""
        with self._lock:
            return {k: MeterTotal(v.call_count, v.est_cu) for k, v in self._totals.items()}

    def drain(self) -> dict[MeterKey, MeterTotal]:
        """取出当前累计值并清空，用于落库；落库失败时调用方应当用 `restore` 放回。"""
        with self._lock:
            drained, self._totals = self._totals, {}
            return drained

    def restore(self, totals: dict[MeterKey, MeterTotal]) -> None:
        """把 `drain` 取出但没能落库的累计值加回去，避免账本丢数。"""
        with self._lock:
            for key, value in totals.items():
                total = self._totals.setdefault(key, MeterTotal())
                total.call_count += value.call_count
                total.est_cu = None if value.est_cu is None or total.est_cu is None else total.est_cu + value.est_cu
