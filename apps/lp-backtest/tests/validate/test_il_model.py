from datetime import date, timedelta

from alpha_metrics.features.volatility import MIN_CLOSE_OBSERVATIONS
from lp_backtest.validate.il_model import collect_samples


def _dated_series(closes: list[float]) -> list[tuple[date, float]]:
    start = date(2026, 1, 1)
    return [(start + timedelta(days=i), c) for i, c in enumerate(closes)]


def test_no_samples_when_history_too_short():
    series = _dated_series([1.0] * (MIN_CLOSE_OBSERVATIONS + 3))
    samples = collect_samples("0xpool", series, t_ref_days=7, stride_days=7)
    assert samples == []  # 30 + 7 = 37 天才够一个样本，这里只给了 33 天


def test_zero_error_for_flat_price():
    # i 从 MIN_CLOSE_OBSERVATIONS-1 开始，需要 i+7 < len(closes)，恰好 37 天只够跑一个样本点。
    total_days = MIN_CLOSE_OBSERVATIONS + 7
    series = _dated_series([1.0] * total_days)
    samples = collect_samples("0xpool", series, t_ref_days=7, stride_days=7)
    assert len(samples) == 1
    sample = samples[0]
    assert sample.sigma_price == 0.0
    assert sample.predicted_il == 0.0
    assert sample.realized_il == 0.0
    assert sample.error == 0.0


def test_realized_il_reflects_actual_price_move():
    total_days = MIN_CLOSE_OBSERVATIONS + 7
    # 预测窗口（前 30 天）价格是平的，只在第 t+7 天（最后一天）翻倍——只影响 realized_il，不影响预测。
    closes = [1.0] * (total_days - 1) + [2.0]
    series = _dated_series(closes)
    samples = collect_samples("0xpool", series, t_ref_days=7, stride_days=7)
    assert len(samples) == 1
    sample = samples[0]
    assert sample.realized_il < 0  # 价格翻倍必然产生无常损失
    assert sample.predicted_il == 0.0  # 预测窗口价格是平的，sigma_price=0
