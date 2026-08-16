"""`_price_from_sqrt`/`_floor_to_minute` 是纯函数，用真实链上验证过的 sqrtPriceX96 值
（跟 packages/protocols/tests/test_pancakeswap_v3.py 里同一条真实 Swap 日志一致）做回归基准。
"""

from datetime import UTC, datetime

from lp_backtest.aggregate_candles import _floor_to_minute, _price_from_sqrt

# 取自 test_pancakeswap_v3.py::test_decode_swap_event_matches_real_bsc_log 解出的真实值。
REAL_SQRT_PRICE_X96 = 315443755530133020918675949


def test_price_from_sqrt_matches_real_btc_usdt_price():
    # BTC/USDT 池子：token0=USDT（18 位小数），token1=BTCB（18 位小数）。
    price = _price_from_sqrt(REAL_SQRT_PRICE_X96, decimals0=18, decimals1=18)
    # price 是 token1/token0（BTC/USDT）汇率，1/price 才是"多少 USDT 换 1 个 BTC"。
    assert 50_000 < 1 / price < 80_000


def test_price_from_sqrt_adjusts_for_mismatched_decimals():
    # decimals 不同时（如 token0 6 位、token1 18 位），换算结果应该按 10^(d0-d1) 缩放，
    # 不能假设两边精度一样——这正是这个函数存在的意义（1c 阶段吃过单位不统一的教训）。
    price_same_decimals = _price_from_sqrt(REAL_SQRT_PRICE_X96, decimals0=18, decimals1=18)
    price_mismatched = _price_from_sqrt(REAL_SQRT_PRICE_X96, decimals0=6, decimals1=18)
    assert price_mismatched == price_same_decimals * 10 ** (6 - 18)


def test_floor_to_minute_truncates_seconds_and_microseconds():
    ts = datetime(2026, 8, 16, 5, 53, 42, 123456, tzinfo=UTC)
    assert _floor_to_minute(ts) == datetime(2026, 8, 16, 5, 53, 0, tzinfo=UTC)
