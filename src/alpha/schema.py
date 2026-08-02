"""Canonical 数据模型：研究侧只读这些结构，不感知交易所原始字段。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

# 当前 schema 版本；字段不兼容变更时递增
SCHEMA_VERSION = 1

Side = Literal["buy", "sell", "unknown"]


class Envelope(BaseModel):
    """所有 canonical 行共享的信封字段。"""

    schema_version: int = SCHEMA_VERSION
    dataset: str
    venue: str
    market_type: str
    instrument_id: str
    symbol_raw: str
    ts_event_ms: int
    ts_ingest_ms: int
    source_seq: Optional[str] = None


class OhlcvBar(Envelope):
    """K 线。ts_event_ms 取交易所给出的开盘时间（open time）。"""

    dataset: str = "ohlcv"
    tf: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: Optional[int] = None
    quote_volume: Optional[float] = None


class TradeTick(Envelope):
    """逐笔/聚合成交。"""

    dataset: str = "trade"
    price: float
    size: float
    side: Side = "unknown"
    trade_id: str
    is_buyer_maker: Optional[bool] = None
    feed: Optional[str] = None  # 美股数据源 iex|sip；crypto 通常为空


class QuoteTick(Envelope):
    """L1 买卖一档报价。"""

    dataset: str = "quote"
    bid_px: float
    bid_sz: float
    ask_px: float
    ask_sz: float
    feed: str


class FundingRate(Envelope):
    """资金费率。funding_rate 为小数，如 0.0001 = 1bp。"""

    dataset: str = "funding"
    funding_rate: float
    mark_price: Optional[float] = None
    index_price: Optional[float] = None
    next_funding_ts_ms: Optional[int] = None


class FundamentalPoint(Envelope):
    """公司基本面长表行。ts_event_ms 取报告期期末（UTC 日起点）。"""

    dataset: str = "fundamental"
    metric: str
    value: float
    unit: Optional[str] = None
    frequency: str
    statement: str  # ic | bs | cf | metric | earnings


class MacroPoint(Envelope):
    """宏观指标长表行。ts_event_ms 取观测期（UTC 日起点）。"""

    dataset: str = "macro"
    metric: str = "value"
    value: float
    unit: Optional[str] = None
    frequency: str


class Instrument(BaseModel):
    """标的注册信息。"""

    instrument_id: str
    venue: str
    market_type: str
    base: str
    quote: str
    settle: Optional[str] = None
    symbol_raw: str
    listed_at: Optional[int] = None
    delisted_at: Optional[int] = None
    meta_json: str = "{}"


def make_instrument_id(venue: str, market_type: str, symbol_raw: str) -> str:
    """生成单源内稳定的 instrument_id。"""
    return f"{venue}:{market_type}:{symbol_raw}"
