"""数据源共用的 HTTP 基础设施：节流、带重试和记账的 GET、熔断器。

只处理技术层面的事（限流、重试、状态码分类、记账），不含任何业务判断；
具体数据源只负责拼请求和映射字段。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from enum import StrEnum

import requests
from alpha_core.errors import DataSourceUnavailableError, SourceRateLimitedError
from alpha_core.metering import CallMeter, CallStatus, NullCallMeter

logger = logging.getLogger(__name__)

USER_AGENT = "alpha-research/0.1 (+wallet-analyzer)"
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_TRANSIENT_STATUS = {500, 502, 503, 504}


def endpoint_label(path: str) -> str:
    """把接口路径里的地址替换成占位符，作为账本的 method 维度（避免每个地址一行）。"""
    return _ADDRESS_RE.sub(":addr", path)


class Throttle:
    """保证相邻两次请求间隔不小于 `min_interval` 秒（线程安全）。"""

    def __init__(self, min_interval: float, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._min = min_interval
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delta = self._min - (time.monotonic() - self._last)
            if delta > 0:
                self._sleep(delta)
            self._last = time.monotonic()


def metered_get(
    session: requests.Session,
    url: str,
    *,
    provider: str,
    method: str,
    params: dict | None = None,
    meter: CallMeter | None = None,
    throttle: Throttle | None = None,
    cu: int | None = 0,
    attempts: int = 3,
    timeout: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    headers: dict | None = None,
) -> requests.Response:
    """发一个 GET：连接错误和 5xx 按指数退避重试；429 也退避重试（公共接口多为短时限流），
    用完次数仍 429 抛 `SourceRateLimitedError`，其他失败抛 `DataSourceUnavailableError`。
    2xx 和其余 4xx（如 404）原样返回，由调用方解释。每次尝试都记账。

    @param cu 每次调用的计费单位；免费接口为 0，未知为 None
    """
    meter = meter or NullCallMeter()
    last: str = ""
    for attempt in range(1, attempts + 1):
        if throttle is not None:
            throttle.wait()
        try:
            resp = session.get(
                url, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT, **(headers or {})}
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            meter.record(provider, method, cu=cu, status=CallStatus.ERROR)
            last = str(exc)
        else:
            if resp.status_code == 429:
                meter.record(provider, method, cu=cu, status=CallStatus.RATE_LIMITED)
                last = "HTTP 429"
                if attempt == attempts:
                    raise SourceRateLimitedError(f"{provider} {method} 限流: {resp.text[:200]}")
            elif resp.status_code in _TRANSIENT_STATUS:
                meter.record(provider, method, cu=cu, status=CallStatus.ERROR)
                last = f"HTTP {resp.status_code}"
            else:
                status = CallStatus.OK if resp.status_code < 400 or resp.status_code == 404 else CallStatus.ERROR
                meter.record(provider, method, cu=cu, status=status)
                return resp
        if attempt < attempts:
            sleep(min(30.0, 2.0 * 2 ** (attempt - 1)))
    raise DataSourceUnavailableError(f"{provider} {method} 重试后仍失败: {last}")


class BreakerState(StrEnum):
    """熔断器状态。"""

    CLOSED = "closed"  # 正常放行
    OPEN = "open"  # 连续失败过多，冷却期内直接跳过该来源
    HALF_OPEN = "half_open"  # 冷却期结束，放行一次试探请求


class CircuitBreaker:
    """按来源维护的熔断器（只在进程内存）：连续失败 `threshold` 次后打开，冷却 `cooldown` 秒后半开试探。"""

    def __init__(self, *, threshold: int = 5, cooldown: float = 300.0, clock: Callable[[], float] = time.monotonic):
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self._cooldown:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow(self) -> bool:
        """是否允许发请求：关闭或半开时允许。"""
        return self.state != BreakerState.OPEN

    def on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def on_failure(self) -> None:
        with self._lock:
            if self._state_locked() == BreakerState.HALF_OPEN:
                self._opened_at = self._clock()  # 试探失败，重新打开
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = self._clock()
                logger.warning("数据源连续失败 %d 次，熔断 %.0f 秒", self._failures, self._cooldown)
