"""小型、可组合的 OHLCV 特征定义。"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from alpha.features.core import FeatureInputs


@dataclass(frozen=True)
class ReturnFeature:
    """按标的计算收盘价的 N 期简单收益率。

    依赖 ``ohlcv`` 输入，输出与 OHLCV 锚点逐行对齐；每个标的前 ``periods``
    行没有足够历史价格，因此结果为缺失值。
    """

    periods: int = 1
    required_datasets: frozenset[str] = frozenset({"ohlcv"})

    @property
    def name(self) -> str:
        return f"return_{self.periods}"

    def compute(self, inputs: FeatureInputs) -> pd.Series:
        bars = inputs["ohlcv"]
        # 同一标的内的当前收盘价 / N 期前收盘价 - 1。
        return bars.groupby("instrument_id", sort=False)["close"].pct_change(self.periods)


@dataclass(frozen=True)
class SmaFeature:
    """按标的计算收盘价的简单移动平均线（SMA）。

    依赖 ``ohlcv`` 输入，输出与 OHLCV 锚点逐行对齐；每个标的前 ``window - 1``
    行窗口不足，因此结果为缺失值。
    """

    window: int
    required_datasets: frozenset[str] = frozenset({"ohlcv"})

    @property
    def name(self) -> str:
        return f"sma_{self.window}"

    def compute(self, inputs: FeatureInputs) -> pd.Series:
        bars = inputs["ohlcv"]
        # 在每个标的内部滚动计算，避免不同标的之间的数据相互混入。
        return bars.groupby("instrument_id", sort=False)["close"].transform(
            lambda close: close.rolling(self.window, min_periods=self.window).mean()
        )
