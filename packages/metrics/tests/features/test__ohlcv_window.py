from datetime import UTC, datetime

from alpha_metrics.features._ohlcv_window import Candle, resample_to_5m


def _candle(minute: int, o: float, h: float, l: float, c: float) -> Candle:  # noqa: E741
    return Candle(ts_event=datetime(2026, 8, 16, 10, minute, tzinfo=UTC), open=o, high=h, low=l, close=c)


def test_resample_aligns_five_aligned_1m_bars_into_one_5m_bar():
    # 10:00-10:04 是一个完整的 5 分钟桶；10:05 是下一个桶的第一根，触发"10:00 桶已经走完"。
    candles = [
        _candle(0, 100, 105, 99, 101),
        _candle(1, 101, 102, 100, 101.5),
        _candle(2, 101.5, 103, 101, 102),
        _candle(3, 102, 104, 101.5, 103),
        _candle(4, 103, 106, 102, 104),
        _candle(5, 104, 104.5, 103.5, 104),
    ]
    result = resample_to_5m(candles)
    assert len(result) == 1
    bar = result[0]
    assert bar.ts_event == datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    assert bar.open == 100
    assert bar.high == 106
    assert bar.low == 99
    assert bar.close == 104


def test_resample_drops_trailing_incomplete_bucket():
    # 只有 10:00-10:02（3 根），10:05 桶还没开始——10:00 桶本身也没走完（最新数据停在 10:02，
    # 桶要到 10:05 才算完），应该整个丢弃，不产出任何K线。
    candles = [_candle(0, 100, 101, 99, 100), _candle(1, 100, 101, 99, 100), _candle(2, 100, 101, 99, 100)]
    assert resample_to_5m(candles) == []


def test_resample_tolerates_gap_within_bucket():
    # 10:00 桶只有 2 根（漏拍 3 根），10:05 桶有数据 -> 10:00 桶判定已经走完，应该产出。
    candles = [_candle(0, 100, 103, 99, 101), _candle(3, 101, 105, 100, 102), _candle(5, 102, 103, 101, 102)]
    result = resample_to_5m(candles)
    assert len(result) == 1
    assert result[0].high == 105
    assert result[0].low == 99


def test_resample_empty_input():
    assert resample_to_5m([]) == []
