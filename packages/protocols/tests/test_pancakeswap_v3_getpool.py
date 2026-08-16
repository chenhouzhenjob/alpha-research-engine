"""`find_pool_by_tokens` 的回归测试：用一个假 adapter 返回真实链上验证过的 getPool 结果，
不发起任何网络请求。真实性验证过程：直连查询 USDT/USDC 0.01% 档位返回的池子地址
（0x92b7807bf19b7dddf89b706143896d05228f3121）与独立通过 GeckoTerminal 查到的同一个池子一致。
"""

from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin

USDT = "0x55d398326f99059ff775485246999027b3197955"
USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
REAL_POOL = "0x92b7807bf19b7dddf89b706143896d05228f3121"


class _FakeAdapterFound:
    """模拟 `Factory.getPool` 返回一个真实存在的池子地址（32 字节左补零）。"""

    def call(self, *, to: str, data: str) -> bytes:
        return bytes(12) + bytes.fromhex(REAL_POOL.removeprefix("0x"))


class _FakeAdapterNotFound:
    """模拟 `Factory.getPool` 返回零地址（该 token 对 + 费率没有池子）。"""

    def call(self, *, to: str, data: str) -> bytes:
        return bytes(32)


def test_find_pool_by_tokens_decodes_real_pool_address():
    plugin = PancakeswapV3Plugin()
    candidate = plugin.find_pool_by_tokens(_FakeAdapterFound(), USDT, USDC, 100)

    assert candidate is not None
    assert candidate.pool_address == REAL_POOL
    assert candidate.token0_address == USDT  # USDT 地址字典序小于 USDC，应作为 token0
    assert candidate.token1_address == USDC
    assert candidate.fee_pips == 100
    assert candidate.tick_spacing == 1
    assert candidate.created_at_block == 0
    assert candidate.created_at is None


def test_find_pool_by_tokens_returns_none_when_pool_does_not_exist():
    plugin = PancakeswapV3Plugin()
    candidate = plugin.find_pool_by_tokens(_FakeAdapterNotFound(), USDT, USDC, 100)

    assert candidate is None
