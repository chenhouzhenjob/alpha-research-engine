"""`read_fee_protocol` / `read_cake_emission` 的回归测试：用真实链上验证过的返回值做固定样本，
不发起任何网络请求。

真实性验证过程：对 USDT/WBNB 0.01% 池子（0x172fcd41e0913e95784454622d1c3724f546f849）直连查询，
`slot0()` word[5] 解出 feeProtocol=(3300, 3300)；`lmPool()`→`lmLiquidity()`→
`MasterChefV3.getLatestPeriodInfo()` 解出 cakePerSecond≈0.0119、lmLiquidity≈6.29e24，
且对已知无 CAKE farm 的早期废弃池子（0xa7619d726f619062d2d2bcadbb2ee1fb1952d6d7）正确返回 None。
"""

import math

from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin

POOL = "0x172fcd41e0913e95784454622d1c3724f546f849"
LM_POOL = "0x0000000000000000000000000000000000001234"

_LM_POOL_SELECTOR = "0x540d4918"
_LIQUIDITY_SELECTOR = "0x1a686502"


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


class _FakeAdapterSlot0:
    """模拟 `slot0()` 返回 7 个字，word[5] 是打包后的 feeProtocol（真实值 3300/3300）。"""

    def call(self, *, to: str, data: str) -> bytes:
        packed_fee_protocol = 3300 | (3300 << 16)
        words = [0, 0, 0, 0, 0, packed_fee_protocol, 0]
        return b"".join(_word(w) for w in words)


class _FakeAdapterCakeEmissionActive:
    """模拟一个真实存在 CAKE farm 的池子：lmPool 非零 → lmLiquidity 非零 → 排放速率非零。

    活跃流动性设为 lmLiquidity 的 2 倍，验证 `lmLiquidityShare` 算出 0.5，而不只是"非零"。
    """

    LM_LIQUIDITY = 6_290_281_666_356_260_000_000_000
    ACTIVE_LIQUIDITY = LM_LIQUIDITY * 2

    def call(self, *, to: str, data: str) -> bytes:
        if to.lower() == POOL and data == _LM_POOL_SELECTOR:
            return _word(int(LM_POOL, 16))
        if to.lower() == POOL and data == _LIQUIDITY_SELECTOR:
            return _word(self.ACTIVE_LIQUIDITY)
        if to.lower() == LM_POOL:
            return _word(self.LM_LIQUIDITY)
        # MasterChefV3.getLatestPeriodInfo(pool)：cakePerSecondRaw + 一个足够远的 endTime
        cake_per_second_raw = 11_895_171_090_984_901_000_000_000
        end_time = 4_000_000_000  # 远未来的时间戳，不会因为测试运行时间而失效
        return _word(cake_per_second_raw) + _word(end_time)


class _FakeAdapterNoFarm:
    """模拟没有挂 CAKE farm 的池子：`lmPool()` 返回零地址。"""

    def call(self, *, to: str, data: str) -> bytes:
        return _word(0)


def test_read_fee_protocol_decodes_packed_value():
    plugin = PancakeswapV3Plugin()
    fee_protocol0, fee_protocol1 = plugin.read_fee_protocol(_FakeAdapterSlot0(), POOL)
    assert fee_protocol0 == 3300
    assert fee_protocol1 == 3300


class _FakeAdapterSlot0PriceAndTick:
    """模拟 `slot0()` 返回真实 BTC/USDT Swap 日志验证过的 sqrtPriceX96/tick 组合
    （跟 test_tick_math.py 用的同一条真实数据，tick=-110528，两个 token 都是 18 位小数）。
    """

    SQRT_PRICE_X96 = 315443755530133020918675949
    TICK = -110528

    def call(self, *, to: str, data: str) -> bytes:
        word0 = self.SQRT_PRICE_X96.to_bytes(32, "big")
        word1 = self.TICK.to_bytes(32, "big", signed=True)
        return word0 + word1 + b"\x00" * 32 * 5  # 后面 5 个字（本方法不关心）随便填零


def test_read_slot0_price_and_tick_decodes_real_btc_usdt_swap():
    plugin = PancakeswapV3Plugin()
    price, tick = plugin.read_slot0_price_and_tick(
        _FakeAdapterSlot0PriceAndTick(), POOL, decimals0=18, decimals1=18
    )
    assert tick == -110528
    assert 50_000 < 1 / price < 80_000


def test_read_cake_emission_when_farm_active():
    plugin = PancakeswapV3Plugin()
    result = plugin.read_cake_emission(_FakeAdapterCakeEmissionActive(), POOL)
    assert result is not None
    cake_per_second, lm_liquidity_share = result
    assert cake_per_second > 0
    assert math.isclose(lm_liquidity_share, 0.5, rel_tol=1e-9)


def test_read_cake_emission_returns_none_when_no_farm():
    plugin = PancakeswapV3Plugin()
    assert plugin.read_cake_emission(_FakeAdapterNoFarm(), POOL) is None
