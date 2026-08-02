"""外部连接器的统一 source 名称注册表。"""

from __future__ import annotations

from typing import Any

from alpha.integrations.market_data import VenueAdapter
from alpha.integrations.providers.alpaca import AlpacaAdapter
from alpha.integrations.venues.binance.market_data import BinanceAdapter
from alpha.integrations.venues.binance.spot_market_data import BinanceSpotAdapter
from alpha.integrations.venues.hyperliquid.market_data import HyperliquidAdapter

MARKET_DATA_ADAPTERS = {
    "binance": BinanceAdapter,
    "binance_spot": BinanceSpotAdapter,
    "hyperliquid": HyperliquidAdapter,
    "alpaca": AlpacaAdapter,
}


def create_market_data_adapter(venue: str, **kwargs: Any) -> VenueAdapter:
    """构造已注册的行情适配器，不向调用方暴露具体导入路径。"""
    try:
        factory = MARKET_DATA_ADAPTERS[venue]
    except KeyError as exc:
        raise ValueError(f"未注册的行情 source: {venue}") from exc
    return factory(**kwargs)
