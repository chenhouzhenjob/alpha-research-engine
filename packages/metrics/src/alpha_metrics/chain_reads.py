"""指标计算需要的链上"当前值"读取，抽出来避免各调用方（`lp_backtest.metrics`/`calibrate.weights`/
`live-signal` 的 `/conclusion` 端点）各写一遍同样的 try/except 兜底逻辑（读失败就标记不可得，
不让单个池子的链上读取失败拖垮整批计算）。

`feeProtocol`/CAKE 排放信息这两个读取加了一层进程内 TTL 缓存（见下方"缓存设计"）——真实跑过
`live-signal` 才发现的问题：`/conclusion` 每次请求都会对候选集里每个池子重新读一遍这两项
（各自最多 1/4 次 `eth_call`），而这两个值本来就变化很慢（`feeProtocol` 是治理参数，
CAKE 排放速率通常按周/双周周期变），高频轮询这个端点会造成大量没有必要的链上调用。
"""

from __future__ import annotations

import logging
import time

from alpha_chains.base import ChainAdapter
from alpha_chains.erc20 import read_decimals
from alpha_core.types import Chain
from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin
from alpha_storage.repositories.tokens import TokenRepository
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 缓存设计：{pool_address: (value, 写入时间戳)}，模块级全局字典，进程内（比如 live-signal 的
# uvicorn 常驻进程）跨请求共享；一次性 CLI 进程里等于没有跨调用效果，但也无害。
# TTL 选 5 分钟——不是精确考证出来的最优值，是"比这两个值实际变化频率短得多、又能大幅减少
# 高频轮询场景下的调用量"之间的一个保守折中，需要更激进可以调大。
_CACHE_TTL_SECONDS = 300
_fee_protocol_cache: dict[str, tuple[tuple[int, int] | None, float]] = {}
_cake_emission_cache: dict[str, tuple[tuple[float, float] | None, float]] = {}

# 价格/tick 变化比 feeProtocol/CAKE 排放快得多，缓存 5 分钟会让退出信号 5（累计已实现 IL）
# 和推荐区间用过期价格——用一个短得多的独立 TTL（阶段 1 确认过的实现决策 #2），不跟上面两个
# 共用同一个 5 分钟缓存字典/常量。
_PRICE_CACHE_TTL_SECONDS = 15
_price_tick_cache: dict[str, tuple[tuple[float, int] | None, float]] = {}


def read_fee_protocol_safe(
    adapter: ChainAdapter, plugin: PancakeswapV3Plugin, pool_address: str
) -> tuple[int, int] | None:
    cached = _fee_protocol_cache.get(pool_address)
    now = time.monotonic()
    if cached is not None and now - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]
    try:
        value = plugin.read_fee_protocol(adapter, pool_address)
    except Exception:
        logger.exception("读取 feeProtocol 失败: %s", pool_address)
        value = None
    _fee_protocol_cache[pool_address] = (value, now)
    return value


def read_cake_emission_safe(
    adapter: ChainAdapter, plugin: PancakeswapV3Plugin, pool_address: str
) -> tuple[float, float] | None:
    cached = _cake_emission_cache.get(pool_address)
    now = time.monotonic()
    if cached is not None and now - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]
    try:
        value = plugin.read_cake_emission(adapter, pool_address)
    except Exception:
        logger.exception("读取 CAKE 排放信息失败: %s", pool_address)
        value = None
    _cake_emission_cache[pool_address] = (value, now)
    return value


def read_slot0_price_and_tick_safe(
    adapter: ChainAdapter, plugin: PancakeswapV3Plugin, pool_address: str, decimals0: int, decimals1: int
) -> tuple[float, int] | None:
    """`read_fee_protocol_safe`/`read_cake_emission_safe` 的同款 try/except 兜底，但用短得多
    的独立 TTL（15 秒，见模块级 `_PRICE_CACHE_TTL_SECONDS` 的注释）。
    """
    cache_key = f"{pool_address}:{decimals0}:{decimals1}"
    cached = _price_tick_cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[1] < _PRICE_CACHE_TTL_SECONDS:
        return cached[0]
    try:
        value = plugin.read_slot0_price_and_tick(adapter, pool_address, decimals0, decimals1)
    except Exception:
        logger.exception("读取 slot0 价格/tick 失败: %s", pool_address)
        value = None
    _price_tick_cache[cache_key] = (value, now)
    return value


def read_decimals_cached(session: Session, adapter: ChainAdapter, chain: Chain, token_address: str) -> int:
    """`decimals()` 是 ERC20 标准里的不可变值，永久缓存在 `tokens` 表（不是进程内存）——
    跟上面两个 TTL 缓存不同，这个值永远不需要过期重查，见 `TokenRepository`/`TokenRow` 的注释。
    """
    repo = TokenRepository(session)
    cached = repo.get_decimals(chain, token_address)
    if cached is not None:
        return cached
    decimals = read_decimals(adapter, token_address)
    repo.upsert(chain, token_address, decimals)
    return decimals
