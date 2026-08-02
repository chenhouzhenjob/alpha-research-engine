"""VenueAdapter 协议：新数据源实现本接口即可接入。"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from alpha.schema import FundingRate, Instrument, OhlcvBar, TradeTick


@runtime_checkable
class VenueAdapter(Protocol):
    """交易所/数据源适配器。"""

    venue: str
    market_type: str

    async def list_instruments(self) -> list[Instrument]:
        """拉取并归一化可交易标的列表。"""
        ...

    async def fetch_ohlcv(
        self,
        symbol_raw: str,
        tf: str,
        start_ms: int,
        end_ms: int,
    ) -> list[OhlcvBar]:
        """回填 K 线；时间窗 [start_ms, end_ms)。"""
        ...

    async def fetch_funding(
        self,
        symbol_raw: str,
        start_ms: int,
        end_ms: int,
    ) -> list[FundingRate]:
        """回填资金费率历史。"""
        ...

    def stream_trades(self, symbol_raw: str) -> AsyncIterator[TradeTick]:
        """订阅实时成交流（断线由调用方或实现方重连）。"""
        ...

    async def close(self) -> None:
        """释放 HTTP/WS 资源。"""
        ...
