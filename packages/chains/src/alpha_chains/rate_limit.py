"""按计费单位（CU）限速的令牌桶。

NodeReal 免费版限制每秒 300 CU，超过会被拒绝（同样消耗重试时间）。批量取回执这类场景
很容易瞬间打满，所以在发请求之前按单价预扣令牌：余额不够就等待。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class CuTokenBucket:
    """容量为 `max_cups`、每秒补满 `max_cups` 的令牌桶；进程内多线程共享一个实例。

    `max_cups` 为 None 表示不限速（默认行为，保证不配置时和原来一致）。
    单次需要的 CU 超过容量时，等桶满后放行并把余额扣成负数，后续请求会相应多等，
    整体速率仍然不超过上限。
    """

    def __init__(
        self,
        max_cups: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_cups is not None and max_cups <= 0:
            raise ValueError("max_cups 必须为正数或 None")
        self._max = max_cups
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = float(max_cups or 0)
        self._last = clock()

    @property
    def enabled(self) -> bool:
        return self._max is not None

    def acquire(self, cu: float) -> float:
        """预扣 `cu` 个令牌，必要时阻塞等待；返回实际等待的秒数（便于测试和日志）。"""
        cap = self._max
        if cap is None or cu <= 0:
            return 0.0
        waited = 0.0
        with self._lock:
            self._refill(cap)
            need = min(cu, cap)  # 超过容量的请求只需要等到桶满
            if self._tokens < need:
                wait = (need - self._tokens) / cap
                self._sleep(wait)
                waited = wait
                self._refill(cap)
            self._tokens -= cu
        return waited

    def _refill(self, cap: float) -> None:
        now = self._clock()
        self._tokens = min(cap, self._tokens + (now - self._last) * cap)
        self._last = now
