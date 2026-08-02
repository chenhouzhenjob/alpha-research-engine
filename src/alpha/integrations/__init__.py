"""外部系统连接器，按平台或数据供应商而非业务流程组织。"""

from alpha.integrations.market_data import VenueAdapter
from alpha.integrations.registry import create_market_data_adapter
from alpha.integrations.providers.alpaca import AlpacaAdapter
from alpha.integrations.providers.finnhub import FinnhubAdapter
from alpha.integrations.providers.fred import FredAdapter
from alpha.integrations.venues.binance.market_data import BinanceAdapter
from alpha.integrations.venues.hyperliquid.market_data import HyperliquidAdapter

__all__ = [
    "AlpacaAdapter", "BinanceAdapter", "FinnhubAdapter", "FredAdapter",
    "HyperliquidAdapter", "VenueAdapter", "create_market_data_adapter",
]
