"""ATR（Average True Range，真实波幅），阶段 1 新增（`live-signal-system-设计方案.md` 第 4 章）。

    TR_t = max(high_t - low_t, |high_t - close_{t-1}|, |low_t - close_{t-1}|)
    ATR_1 = mean(TR_1..TR_N)                       （首个值：前 N 个 TR 的简单平均）
    ATR_t = (ATR_{t-1} * (N-1) + TR_t) / N          （之后：Wilder 平滑）

衡量短周期真实波幅，跟 `volatility.sigma_price`（基于日线收盘价对数收益率）是两个不同粒度的
波动率信号，互补不替代——σ_price 覆盖"慢"的一端，ATR 覆盖"快"的一端。

**用 5 分钟K线，不用落库的原始 1 分钟K线**：1 分钟粒度会被链上出块噪音主导，对做市调仓这种
日级别的决策没有意义。调用方用 `_ohlcv_window.resample_to_5m` 把 1 分钟K线重采样成 5 分钟
再传进来，这里不做重采样，只做 ATR 计算——保持这个函数纯粹（跟 `sigma_price` 一样，只认输入
序列，不关心序列怎么来的）。

`DEFAULT_PERIOD = 14` 是 Wilder 指标的惯用值——设计文档只说"标准 TA 指标"，没给具体周期，
这是默认假设，不是文档明确要求的数字。
"""

from __future__ import annotations

from alpha_core.metrics import MetricValue

from ._ohlcv_window import Candle

DEFAULT_PERIOD = 14


def atr(candles: list[Candle], *, period: int = DEFAULT_PERIOD) -> MetricValue[float]:
    """@param candles 按时间升序排列的 5 分钟 OHLC K 线（`resample_to_5m` 的输出）
    @returns 不足 `period+1` 根 K 线（需要 period 个 TR，每个 TR 需要一个"前一根收盘价"，
    所以至少要 period+1 根原始K线）时标记 unavailable
    """
    if len(candles) < period + 1:
        return MetricValue.unavailable(
            f"5分钟K线不足 {period + 1} 根（现有 {len(candles)} 根，Wilder ATR 需要这么多才能算出首个真实波幅均值）"
        )

    true_ranges = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i].high, candles[i].low, candles[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    current_atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        current_atr = (current_atr * (period - 1) + tr) / period

    return MetricValue.available(current_atr)


def atr_pct_series(candles: list[Candle], *, period: int = DEFAULT_PERIOD) -> list[float]:
    """`models.trend_state.classify_volatility` 要跟"这个池子自己的历史"比较，需要一段
    ATR 相对价格的百分比序列，不是单个当前值——这里对每根K线（从第 `period` 根开始）用它
    之前 `period+1` 根K线滚动算一次 ATR，除以那根K线的收盘价，拼成序列。

    @returns 长度 `max(0, len(candles) - period)`；最后一个元素是"当前"ATR%，
    前面的是历史——调用方通常用 `series[-1]` 当 `atr_pct_now`、`series[:-1]` 当
    `atr_pct_history`（`trend_state.classify_volatility` 的两个参数）。
    """
    series = []
    for i in range(period, len(candles)):
        window = candles[i - period : i + 1]
        window_atr = atr(window, period=period)
        if window_atr.is_available and window[-1].close:
            series.append(window_atr.value / window[-1].close)
    return series
