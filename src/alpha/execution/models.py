"""纸面执行与真实执行共用的交易所无关订单和风控模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OrderStatus(str, Enum):
    FILLED = "filled"
    REJECTED = "rejected"
    SKIPPED_NO_PRICE = "skipped_no_price"


@dataclass(frozen=True)
class RiskLimits:
    max_gross_leverage: float = 1.0


@dataclass(frozen=True)
class OrderRequest:
    ts_signal_ms: int
    ts_fill_ms: int
    instrument_id: str
    target_weight: float
    quantity: float
    reference_price: float
