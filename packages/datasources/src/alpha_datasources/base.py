"""行情类数据源的抽象接口。新增数据源（如 Subgraph/Dune）实现本接口即可接入。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class PoolMarketSnapshot:
    """某一时刻从数据源读到的池子行情快照（当前值，不是历史序列）。"""

    pool_address: str
    tvl_usd: float | None  # 池子总锁仓价值（USD），数据源缺失时为 None
    volume_24h_usd: float | None  # 滚动 24h 交易量（USD）
    close_price: float | None  # 当前 base/quote 汇率，口径与 OHLCV close 一致
    created_at: datetime | None  # 数据源报告的池子创建时间；不是链上权威值，仅在拿不到区块号时兜底用
    name: str | None  # 数据源报告的交易对显示名（如 "CAKE / WBNB 0.25%"），仅供人读，不参与任何计算
    fetched_at: datetime


@dataclass(frozen=True)
class OhlcvPoint:
    """一根日线 K 线。"""

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float  # 计价货币口径的成交量（见各数据源实现说明其具体单位）


@dataclass(frozen=True)
class MinuteOhlcvPoint:
    """一根分钟线 K 线。跟 `OhlcvPoint`（日线）分开定义——字段名 `ts_event`（不是 `day`），
    因为它最终流向的是 `pool_ohlcv` 表（`instrument_id`+`tf`+`ts_event` 体系），跟日线流向的
    `pool_metrics_history`（`chain`+`pool_address`+`snapshot_date`）是两套独立的下游，
    不是同一个概念缩小粒度。不放进 `MarketDataSource` 抽象接口——不是所有数据源都保证有
    分钟级数据，见 `GeckoTerminalClient.get_minute_ohlcv`。
    """

    ts_event: datetime  # K 线开盘时间
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketDataSource(ABC):
    """行情数据源统一接口。"""

    @abstractmethod
    def get_pool_snapshots(self, pool_addresses: list[str]) -> dict[str, PoolMarketSnapshot]:
        """批量获取池子的当前行情快照。

        @param pool_addresses 池子地址列表
        @returns {池子地址: 快照}；数据源未覆盖到的地址不出现在返回值里（而不是填 None 占位）
        """

    @abstractmethod
    def get_daily_ohlcv(self, pool_address: str, *, days: int) -> list[OhlcvPoint]:
        """获取某个池子最近 `days` 天的日线 K 线，按日期升序返回。"""
