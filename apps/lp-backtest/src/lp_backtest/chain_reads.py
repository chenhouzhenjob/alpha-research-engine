"""1b 指标计算需要的链上"当前值"读取，抽出来避免 `metrics.py`/`overview.py` 各写一遍同样的
try/except 兜底逻辑（读失败就标记不可得，不让单个池子的链上读取失败拖垮整批计算）。
"""

from __future__ import annotations

import logging

from alpha_chains.base import ChainAdapter
from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin

logger = logging.getLogger(__name__)


def read_fee_protocol_safe(
    adapter: ChainAdapter, plugin: PancakeswapV3Plugin, pool_address: str
) -> tuple[int, int] | None:
    try:
        return plugin.read_fee_protocol(adapter, pool_address)
    except Exception:
        logger.exception("读取 feeProtocol 失败: %s", pool_address)
        return None


def read_cake_emission_safe(
    adapter: ChainAdapter, plugin: PancakeswapV3Plugin, pool_address: str
) -> tuple[float, float] | None:
    try:
        return plugin.read_cake_emission(adapter, pool_address)
    except Exception:
        logger.exception("读取 CAKE 排放信息失败: %s", pool_address)
        return None
