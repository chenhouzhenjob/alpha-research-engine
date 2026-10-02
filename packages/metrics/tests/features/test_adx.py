from datetime import UTC, datetime

from alpha_metrics.features._ohlcv_window import Candle
from alpha_metrics.features.adx import DEFAULT_PERIOD, adx


def _uptrend_candle(i: int) -> Candle:
    # 纯单边上涨：high=100+2i, low=98+2i（区间恒为2）, close=99+2i（中点）。
    # 手算：+DM=2 恒定，-DM=0 恒定，TR=3 恒定（见测试文件顶部推导）——
    # +DI=100*2/3≈66.67，-DI=0，DX=100*|66.67-0|/(66.67+0)=100 恒定，
    # 所以 ADX 应该精确等于 100（对一个恒定为 100 的序列做任何平滑结果还是 100）。
    return Candle(
        ts_event=datetime(2026, 8, 16, tzinfo=UTC), open=99 + 2 * i, high=100 + 2 * i, low=98 + 2 * i, close=99 + 2 * i
    )


def test_adx_unavailable_below_two_period():
    candles = [_uptrend_candle(i) for i in range(2 * DEFAULT_PERIOD - 1)]  # 差 1 根
    result = adx(candles)
    assert not result.is_available
    assert "28" in result.reason  # 2*period


def test_adx_pure_uptrend_gives_max_strength():
    # 恰好 2*period 根，边界情况：应该正好产出唯一一个 ADX 值。
    candles = [_uptrend_candle(i) for i in range(2 * DEFAULT_PERIOD)]
    result = adx(candles)
    assert result.is_available
    assert result.value == 100.0


def test_adx_flat_price_gives_zero_strength():
    # 完全不动的价格：+DM=-DM=0，TR>0（open高低差还在），DI 双方都是 0，DX=0，ADX=0。
    candles = [
        Candle(ts_event=datetime(2026, 8, 16, tzinfo=UTC), open=100, high=101, low=99, close=100)
        for _ in range(2 * DEFAULT_PERIOD)
    ]
    result = adx(candles)
    assert result.is_available
    assert result.value == 0.0
