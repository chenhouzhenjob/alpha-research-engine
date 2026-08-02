"""策略协议与通用目标权重校验。"""

from __future__ import annotations

from typing import Protocol

import pandas as pd


class Strategy(Protocol):
    name: str

    def target_weights(self, bars: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame: ...


def validate_target_weights(weights: pd.DataFrame) -> pd.DataFrame:
    required = {"ts_event_ms", "instrument_id", "target_weight"}
    missing = required - set(weights.columns)
    if missing:
        raise ValueError(f"目标权重缺少字段: {sorted(missing)}")
    if weights["target_weight"].isna().any():
        raise ValueError("目标权重不能为 null")
    return weights.sort_values(["ts_event_ms", "instrument_id"]).reset_index(drop=True)
