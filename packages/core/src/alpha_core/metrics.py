"""指标计算结果的统一值对象。

沿用 alpha-lp `AprEstimateView` 的三态约定（见 `types.MetricAvailability`）：
指标计算函数一律返回 `MetricValue`，不能用 `None`/`0` 静默代表"算不出来"，
调用方（打分、报告、序列化）必须显式处理三种状态，不能只看 `value`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from .types import MetricAvailability

T = TypeVar("T")


@dataclass(frozen=True)
class MetricValue(Generic[T]):
    """一个指标的计算结果。"""

    availability: MetricAvailability
    value: T | None = None  # `AVAILABLE` 时为实际值；`NO_INCENTIVE`/`UNAVAILABLE` 时必须为 None
    reason: str | None = None  # `NO_INCENTIVE`/`UNAVAILABLE` 时的具体原因（如"回看窗口不足 30 天"），便于排查

    def __post_init__(self) -> None:
        if self.availability == MetricAvailability.AVAILABLE and self.value is None:
            raise ValueError("availability=AVAILABLE 时 value 不能为 None")
        if self.availability != MetricAvailability.AVAILABLE and self.value is not None:
            raise ValueError(f"availability={self.availability} 时 value 必须为 None，不能静默带值")

    @classmethod
    def available(cls, value: T) -> MetricValue[T]:
        return cls(availability=MetricAvailability.AVAILABLE, value=value)

    @classmethod
    def unavailable(cls, reason: str) -> MetricValue[T]:
        return cls(availability=MetricAvailability.UNAVAILABLE, reason=reason)

    @classmethod
    def no_incentive(cls, reason: str = "当前无激励") -> MetricValue[T]:
        return cls(availability=MetricAvailability.NO_INCENTIVE, reason=reason)

    @property
    def is_available(self) -> bool:
        return self.availability == MetricAvailability.AVAILABLE
