"""ADX（Average Directional Index，趋势强弱指标），阶段 1 新增（`live-signal-system-设计方案.md`
第 4 章）。判断当前是震荡市还是单边趋势，喂给 `models.trend_state` 的状态机——弱趋势（震荡）
适合窄区间吃手续费，强趋势（单边）应该放宽区间或直接退出。

标准 Wilder ADX 链路：

    +DM_t = high_t - high_{t-1}（大于 -DM 且为正才算，否则记 0）
    -DM_t = low_{t-1} - low_t（大于 +DM 且为正才算，否则记 0）
    TR_t  = max(high_t-low_t, |high_t-close_{t-1}|, |low_t-close_{t-1}|)   （跟 atr.py 同一个 TR）
    对 +DM/-DM/TR 三个序列各自做一遍 Wilder 平滑（跟 atr.py 完全一样的算法：首个值是前 N 个的
    简单平均，之后 `smoothed_t = (smoothed_{t-1}*(N-1) + raw_t) / N`）
    +DI_t = 100 * smoothed(+DM)_t / smoothed(TR)_t
    -DI_t = 100 * smoothed(-DM)_t / smoothed(TR)_t
    DX_t  = 100 * |+DI_t - -DI_t| / (+DI_t + -DI_t)
    ADX   = 对 DX 序列再做一次同样的 Wilder 平滑

**用平滑"平均值"而不是 Wilder 原始惯用的"平滑总和"**：+DI/-DI 是两个同样做法的平滑序列的比值，
分子分母的周期缩放因子会互相抵消，用平均值还是总和数值上等价，这里选平均值是为了和 `atr.py`
共用完全同一套递推公式，不是发明新算法。

**需要约 `2*period` 根 5 分钟K线**（不是 `period+1`）：DX 自己要先经过一段预热期才稳定
（`period` 根K线才能算出第一个 +DI/-DI/DX），再在 DX 序列上做一次平滑才是 ADX（又需要
`period` 个 DX 值），两层平滑叠加，所以门槛是 ATR 的两倍。
"""

from __future__ import annotations

from alpha_core.metrics import MetricValue

from ._ohlcv_window import Candle

DEFAULT_PERIOD = 14


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """对一列原始值做 Wilder 平滑，返回平滑后的序列（长度 = `len(values) - period + 1`，
    从第 `period` 个原始值开始才有第一个平滑值）。`values` 长度不足 `period` 时返回空列表——
    调用方（`adx`）已经用 K 线数量提前判断过 unavailable，这里不重复报错，只是安全地返回空。
    """
    if len(values) < period:
        return []
    smoothed = [sum(values[:period]) / period]
    for v in values[period:]:
        smoothed.append((smoothed[-1] * (period - 1) + v) / period)
    return smoothed


def adx(candles: list[Candle], *, period: int = DEFAULT_PERIOD) -> MetricValue[float]:
    """@param candles 按时间升序排列的 5 分钟 OHLC K 线（`resample_to_5m` 的输出）
    @returns 不足 `2*period` 根 K 线时标记 unavailable（见模块文档"需要约 2*period 根"）
    """
    min_candles = 2 * period
    if len(candles) < min_candles:
        return MetricValue.unavailable(
            f"5分钟K线不足 {min_candles} 根（现有 {len(candles)} 根，ADX 需要两层平滑，"
            f"门槛是 ATR 的两倍）"
        )

    plus_dm, minus_dm, true_ranges = [], [], []
    for i in range(1, len(candles)):
        high, low = candles[i].high, candles[i].low
        prev_high, prev_low, prev_close = candles[i - 1].high, candles[i - 1].low, candles[i - 1].close
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    smoothed_plus_dm = _wilder_smooth(plus_dm, period)
    smoothed_minus_dm = _wilder_smooth(minus_dm, period)
    smoothed_tr = _wilder_smooth(true_ranges, period)

    dx_series = []
    for pdm, mdm, tr in zip(smoothed_plus_dm, smoothed_minus_dm, smoothed_tr, strict=True):
        if tr == 0:
            dx_series.append(0.0)  # 没有真实波幅（极端情况：价格完全不动），趋势强度记 0
            continue
        plus_di = 100 * pdm / tr
        minus_di = 100 * mdm / tr
        di_sum = plus_di + minus_di
        dx_series.append(0.0 if di_sum == 0 else 100 * abs(plus_di - minus_di) / di_sum)

    smoothed_dx = _wilder_smooth(dx_series, period)
    if not smoothed_dx:
        # 理论上不会走到这——min_candles 的判断已经保证了 dx_series 长度 >= period。
        # 留这个兜底是防御性的，不是预期路径。
        return MetricValue.unavailable("DX 序列不足以平滑出 ADX（未预期的中间状态）")

    return MetricValue.available(smoothed_dx[-1])
