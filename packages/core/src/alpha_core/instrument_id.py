"""标的 ID 构造。格式对齐 `alpha-research-engine`（一个专门的离线行情研究平台）的
`instrument_id = "{venue}:{market_type}:{symbol_raw}"` 约定，方便以后互通，见
research/docs/live-signal-system-设计方案.md"对齐 alpha-research-engine 命名约定"一节。
"""

from __future__ import annotations


class MarketType:
    """标的的市场类型。是开放字符串，不是枚举——`alpha-research-engine` 自己的约定就是
    "market_type 是开放字符串，要新值就加"，这里不破例，只在这里集中收敛已知取值的拼写，
    避免各处手打字符串拼错。
    """

    DEX_POOL = "dex_pool"  # 链上 DEX 池子，本期唯一取值


def build_instrument_id(*, venue: str, market_type: str, symbol_raw: str) -> str:
    """@returns `"{venue}:{market_type}:{symbol_raw}"`，如 `"pancakeswap-v3-bsc:dex_pool:0x..."`"""
    return f"{venue}:{market_type}:{symbol_raw}"
