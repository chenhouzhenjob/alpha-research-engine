"""`tick_math` 是纯函数，用真实链上验证过的值做回归基准（不发起任何网络请求）。"""

from alpha_protocols.tick_math import price_from_sqrt_price_x96, price_to_tick, round_to_tick_spacing

# 跟 packages/protocols/tests/test_pancakeswap_v3.py::test_decode_swap_event_matches_real_bsc_log
# 解出的同一条真实 BTC/USDT Swap 日志一致：sqrtPriceX96=315443755530133020918675949 时，
# 链上真实 tick_after == -110528（两个 token 都是 18 位小数）。
REAL_SQRT_PRICE_X96 = 315443755530133020918675949
REAL_TICK = -110528


def test_price_to_tick_round_trips_real_btc_usdt_swap():
    price = price_from_sqrt_price_x96(REAL_SQRT_PRICE_X96, decimals0=18, decimals1=18)
    assert price_to_tick(price, decimals0=18, decimals1=18) == REAL_TICK


def test_price_to_tick_adjusts_for_mismatched_decimals():
    # decimals 不同的场景：不能假设两边精度一样（跟 _price_from_sqrt 的同名测试是同一个道理）。
    price_same_decimals = price_from_sqrt_price_x96(REAL_SQRT_PRICE_X96, decimals0=18, decimals1=18)
    price_mismatched = price_from_sqrt_price_x96(REAL_SQRT_PRICE_X96, decimals0=6, decimals1=18)
    assert price_mismatched == price_same_decimals * 10 ** (6 - 18)
    # 换算成人类可读价格后再转 tick，即便 decimals 不同也应该还原出同一个真实 tick——
    # tick 是链上原始（未调整精度）价格比的对数，decimals 只影响"人类可读价格"这一层。
    assert price_to_tick(price_mismatched, decimals0=6, decimals1=18) == REAL_TICK


def test_price_to_tick_hand_computed_example():
    # 手算：raw_ratio=1.0001 时，log_1.0001(1.0001) 恰好等于 1。
    assert price_to_tick(1.0001, decimals0=18, decimals1=18) == 1
    # decimals0-decimals1=12 时，price 需要乘 10^12 才能得到同样的 raw_ratio=1.0001。
    assert price_to_tick(1.0001 * 10**12, decimals0=18, decimals1=6) == 1


def test_round_to_tick_spacing():
    assert round_to_tick_spacing(7, 10) == 10
    assert round_to_tick_spacing(4, 10) == 0
    assert round_to_tick_spacing(-7, 10) == -10
    assert round_to_tick_spacing(-110528, 10) == -110530
