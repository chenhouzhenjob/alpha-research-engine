"""币安现货原生 REST / WebSocket 行情适配器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets
from tenacity import retry, stop_after_attempt, wait_exponential
from websockets.exceptions import WebSocketException

from alpha.collection import RawArchive
from alpha.integrations.venues.binance.market_data import _TF_MS, parse_kline, parse_trade_event
from alpha.schema import FundingRate, Instrument, OhlcvBar, TradeTick, make_instrument_id


class BinanceSpotAdapter:
    """币安现货：OHLCV、标的与实时成交；现货没有资金费率。"""

    venue = "binance"

    def __init__(
        self,
        *,
        market_type: str = "spot",
        rest_base: str = "https://api.binance.com",
        ws_base: str = "wss://stream.binance.com:9443",
        symbols: list[str] | None = None,
        raw: RawArchive | None = None,
        timeout: float = 30.0,
    ) -> None:
        if market_type != "spot":
            raise ValueError("BinanceSpotAdapter 仅支持 market_type=spot")
        self.market_type = market_type
        self.rest_base = rest_base.rstrip("/")
        self.ws_base = ws_base.rstrip("/")
        self.symbols = symbols or []
        self.raw = raw
        self._client = httpx.AsyncClient(base_url=self.rest_base, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=0.5, min=0.5, max=8))
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.get(path, params=params)
        if response.status_code in {418, 429}:
            raise httpx.HTTPStatusError("rate limited", request=response.request, response=response)
        response.raise_for_status()
        data = response.json()
        if self.raw is not None:
            self.raw.append(self.venue, path.strip("/").replace("/", "_"), {
                "params": params, "data": data,
            })
        return data

    async def list_instruments(self) -> list[Instrument]:
        data = await self._get("/api/v3/exchangeInfo")
        wanted = set(self.symbols) if self.symbols else None
        result: list[Instrument] = []
        for item in data.get("symbols", []):
            symbol = str(item["symbol"])
            if item.get("status") != "TRADING" or (wanted is not None and symbol not in wanted):
                continue
            result.append(Instrument(
                instrument_id=make_instrument_id(self.venue, self.market_type, symbol),
                venue=self.venue,
                market_type=self.market_type,
                base=str(item.get("baseAsset", "")),
                quote=str(item.get("quoteAsset", "")),
                symbol_raw=symbol,
                meta_json=json.dumps({
                    "baseAssetPrecision": item.get("baseAssetPrecision"),
                    "quotePrecision": item.get("quotePrecision"),
                }, ensure_ascii=False),
            ))
        return result

    async def fetch_ohlcv(
        self, symbol_raw: str, tf: str, start_ms: int, end_ms: int
    ) -> list[OhlcvBar]:
        if tf not in _TF_MS:
            raise ValueError(f"不支持的 timeframe: {tf}")
        bars: list[OhlcvBar] = []
        cursor = start_ms
        limit = 1000
        while cursor < end_ms:
            batch = await self._get("/api/v3/klines", {
                "symbol": symbol_raw, "interval": tf, "startTime": cursor,
                "endTime": end_ms, "limit": limit,
            })
            if not batch:
                break
            bars.extend(
                parse_kline(row, symbol_raw=symbol_raw, tf=tf, market_type=self.market_type)
                for row in batch if int(row[0]) < end_ms
            )
            next_cursor = int(batch[-1][0]) + _TF_MS[tf]
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < limit:
                break
            await asyncio.sleep(0.05)
        return bars

    async def fetch_funding(
        self, symbol_raw: str, start_ms: int, end_ms: int
    ) -> list[FundingRate]:
        """现货没有资金费率；满足统一采集协议。"""
        return []

    async def stream_trades(self, symbol_raw: str) -> AsyncIterator[TradeTick]:
        url = f"{self.ws_base}/ws/{symbol_raw.lower()}@trade"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=60) as websocket:
                    async for raw_message in websocket:
                        message = json.loads(raw_message)
                        if self.raw is not None:
                            self.raw.append(self.venue, "ws_trade", message)
                        yield parse_trade_event(
                            message, symbol_raw=symbol_raw, market_type=self.market_type
                        )
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException):
                await asyncio.sleep(1.0)
