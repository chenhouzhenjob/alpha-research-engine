"""供示例研究流程使用的参考策略。"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from alpha.strategies.core import validate_target_weights


@dataclass(frozen=True)
class MovingAverageCrossStrategy:
    fast_feature: str = "sma_10"
    slow_feature: str = "sma_20"
    long_only: bool = True
    name: str = "moving_average_cross"

    def target_weights(self, bars: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
        frame = features[["ts_event_ms", "instrument_id", self.fast_feature, self.slow_feature]].copy()
        long = frame[self.fast_feature] > frame[self.slow_feature]
        frame["target_weight"] = long.astype(float)
        if not self.long_only:
            frame.loc[~long, "target_weight"] = -1.0
        return validate_target_weights(frame[["ts_event_ms", "instrument_id", "target_weight"]])
