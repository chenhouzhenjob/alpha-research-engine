from datetime import UTC, datetime

from alpha_metrics.features._ohlcv_window import Candle
from alpha_metrics.features.atr import DEFAULT_PERIOD, atr, atr_pct_series


def _flat_candle(i: int) -> Candle:
    # high-low=2, close=open=100 -> TR = max(2, |101-100|, |99-100|) = 2，每根都一样，
    # 方便手算期望的 ATR（跟测试断言的期望值是独立算出来的，不是照抄实现）。
    return Candle(ts_event=datetime(2026, 8, 16, tzinfo=UTC), open=100, high=101, low=99, close=100)


def test_atr_unavailable_below_period_plus_one():
    result = atr([_flat_candle(i) for i in range(DEFAULT_PERIOD)])  # 14 根，差 1 根
    assert not result.is_available
    assert "15" in result.reason  # period+1


def test_atr_first_value_is_simple_mean_of_true_ranges():
    # 恰好 15 根（period+1）：14 个 TR，每个都是 2 -> 首个 ATR = mean = 2.0，没有后续平滑步骤。
    candles = [_flat_candle(i) for i in range(DEFAULT_PERIOD + 1)]
    result = atr(candles, period=DEFAULT_PERIOD)
    assert result.is_available
    assert result.value == 2.0


def test_atr_wilder_smoothing_step():
    # 15 根 flat（首个 ATR=2.0，见上），第 16 根故意做一根大波幅K线：
    # high=120, low=100 -> TR_16 = max(20, |120-100|, |100-100|) = 20
    # Wilder: ATR_16 = (2.0*(14-1) + 20) / 14 = (26+20)/14 = 46/14
    candles = [_flat_candle(i) for i in range(DEFAULT_PERIOD + 1)]
    candles.append(
        Candle(ts_event=datetime(2026, 8, 16, tzinfo=UTC), open=100, high=120, low=100, close=110)
    )
    result = atr(candles, period=DEFAULT_PERIOD)
    assert result.is_available
    assert result.value == 46 / 14


def test_atr_pct_series_empty_when_not_enough_candles():
    assert atr_pct_series([_flat_candle(i) for i in range(DEFAULT_PERIOD)]) == []


def test_atr_pct_series_flat_price_gives_constant_ratio():
    # 恒定 flat K线：每个滚动窗口 ATR 都是 2.0，收盘价恒为 100 -> ATR% 恒为 0.02。
    # 20 根总共产出 20-14=6 个值。
    candles = [_flat_candle(i) for i in range(20)]
    series = atr_pct_series(candles, period=DEFAULT_PERIOD)
    assert len(series) == 6
    assert all(v == 0.02 for v in series)
