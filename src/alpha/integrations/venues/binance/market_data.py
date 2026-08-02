"""币安 USDT-M 永续原生 REST / WS 适配器（不经 CCXT）。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx
import websockets
from tenacity import retry, stop_after_attempt, wait_exponential

from alpha.schema import (
    FundingRate,
    Instrument,
    OhlcvBar,
    TradeTick,
    make_instrument_id,
)
from alpha.collection import RawArchive

# 币安 interval → 毫秒
_TF_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,
}


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def parse_kline(row: list[Any], *, symbol_raw: str, tf: str, market_type: str) -> OhlcvBar:
    """解析 /fapi/v1/klines 单根数组为 OhlcvBar。"""
    # [openTime, o, h, l, c, vol, closeTime, quoteVol, trades, ...]
    return OhlcvBar(
        venue="binance",
        market_type=market_type,
        instrument_id=make_instrument_id("binance", market_type, symbol_raw),
        symbol_raw=symbol_raw,
        ts_event_ms=int(row[0]),
        ts_ingest_ms=_now_ms(),
        source_seq=str(row[0]),
        tf=tf,
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        trade_count=int(row[8]) if len(row) > 8 else None,
        quote_volume=float(row[7]) if len(row) > 7 else None,
    )


def parse_funding(row: dict[str, Any], *, market_type: str) -> FundingRate:
    """解析 /fapi/v1/fundingRate 单条。"""
    symbol_raw = str(row["symbol"])
    mark = row.get("markPrice")
    return FundingRate(
        venue="binance",
        market_type=market_type,
        instrument_id=make_instrument_id("binance", market_type, symbol_raw),
        symbol_raw=symbol_raw,
        ts_event_ms=int(row["fundingTime"]),
        ts_ingest_ms=_now_ms(),
        source_seq=str(row["fundingTime"]),
        funding_rate=float(row["fundingRate"]),
        mark_price=float(mark) if mark not in (None, "") else None,
    )


def parse_trade_event(msg: dict[str, Any], *, symbol_raw: str, market_type: str) -> TradeTick:
    """
    解析 WS 成交事件。

    合约 fstream 上 `@aggTrade` 在部分网络环境无推送，默认用 `@trade`；
    同时兼容 combined stream 的 `{stream,data}` 包装与 spot `@aggTrade`。
    """
    data = msg.get("data", msg)
    buyer_maker = bool(data.get("m"))
    # aggTrade 用 a；逐笔 trade 用 t
    trade_id = str(data["a"] if "a" in data else data["t"])
    return TradeTick(
        venue="binance",
        market_type=market_type,
        instrument_id=make_instrument_id("binance", market_type, symbol_raw),
        symbol_raw=symbol_raw,
        ts_event_ms=int(data["T"]),
        ts_ingest_ms=_now_ms(),
        source_seq=trade_id,
        price=float(data["p"]),
        size=float(data["q"]),
        side="sell" if buyer_maker else "buy",
        trade_id=trade_id,
        is_buyer_maker=buyer_maker,
    )


# 兼容旧名
parse_agg_trade = parse_trade_event


class BinanceAdapter:
    """币安 USDT-M 永续。"""

    venue = "binance"

    def __init__(
        self,
        *,
        market_type: str = "perp",
        rest_base: str = "https://fapi.binance.com",
        ws_base: str = "wss://fstream.binance.com",
        symbols: list[str] | None = None,
        raw: RawArchive | None = None,
        timeout: float = 30.0,
    ) -> None:
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
        """GET 并在限流时退避重试。"""
        resp = await self._client.get(path, params=params)
        if resp.status_code == 429 or resp.status_code == 418:
            # 触发 tenacity 重试
            raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
        resp.raise_for_status()
        data = resp.json()
        if self.raw is not None:
            self.raw.append(self.venue, path.strip("/").replace("/", "_"), {"params": params, "data": data})
        return data

    async def list_instruments(self) -> list[Instrument]:
        """从 exchangeInfo 拉取永续合约元数据。"""
        data = await self._get("/fapi/v1/exchangeInfo")
        out: list[Instrument] = []
        want = set(self.symbols) if self.symbols else None
        for s in data.get("symbols", []):
            if s.get("contractType") != "PERPETUAL":
                continue
            if s.get("status") != "TRADING":
                continue
            symbol_raw = s["symbol"]
            if want is not None and symbol_raw not in want:
                continue
            out.append(
                Instrument(
                    instrument_id=make_instrument_id(self.venue, self.market_type, symbol_raw),
                    venue=self.venue,
                    market_type=self.market_type,
                    base=s.get("baseAsset", ""),
                    quote=s.get("quoteAsset", ""),
                    settle=s.get("marginAsset"),
                    symbol_raw=symbol_raw,
                    meta_json=json.dumps(
                        {
                            "pricePrecision": s.get("pricePrecision"),
                            "quantityPrecision": s.get("quantityPrecision"),
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        return out

    async def fetch_ohlcv(
        self,
        symbol_raw: str,
        tf: str,
        start_ms: int,
        end_ms: int,
    ) -> list[OhlcvBar]:
        """分页拉取 klines；ts_event_ms = openTime。不含未收盘的最后一根由调用方过滤亦可。"""
        if tf not in _TF_MS:
            raise ValueError(f"不支持的 timeframe: {tf}")
        bars: list[OhlcvBar] = []
        cursor = start_ms
        limit = 1500
        while cursor < end_ms:
            batch = await self._get(
                "/fapi/v1/klines",
                {
                    "symbol": symbol_raw,
                    "interval": tf,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": limit,
                },
            )
            if not batch:
                break
            for row in batch:
                open_time = int(row[0])
                if open_time >= end_ms:
                    continue
                bars.append(
                    parse_kline(row, symbol_raw=symbol_raw, tf=tf, market_type=self.market_type)
                )
            last_open = int(batch[-1][0])
            next_cursor = last_open + _TF_MS[tf]
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < limit:
                break
            await asyncio.sleep(0.05)
        return bars

    async def fetch_funding(
        self,
        symbol_raw: str,
        start_ms: int,
        end_ms: int,
    ) -> list[FundingRate]:
        """分页拉取历史资金费率。"""
        out: list[FundingRate] = []
        cursor = start_ms
        limit = 1000
        while cursor < end_ms:
            batch = await self._get(
                "/fapi/v1/fundingRate",
                {
                    "symbol": symbol_raw,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": limit,
                },
            )
            if not batch:
                break
            for row in batch:
                ts = int(row["fundingTime"])
                if ts >= end_ms:
                    continue
                out.append(parse_funding(row, market_type=self.market_type))
            last = int(batch[-1]["fundingTime"])
            next_cursor = last + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < limit:
                break
            await asyncio.sleep(0.05)

        # 补充当前 mark / index（不作为历史行，仅写入最新快照若窗口含现在）
        if end_ms >= _now_ms() - 60_000:
            try:
                prem = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol_raw})
                # 用下次资金时间作事件时间的参考，不覆盖历史
                _ = prem
            except Exception:
                pass
        return out

    async def stream_trades(self, symbol_raw: str) -> AsyncIterator[TradeTick]:
        """订阅合约 `@trade`（见 Source Quirks），断线自动重连。"""
        stream = f"{symbol_raw.lower()}@trade"
        url = f"{self.ws_base}/ws/{stream}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=60) as ws:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if self.raw is not None:
                            self.raw.append(self.venue, "ws_trade", msg)
                        yield parse_trade_event(
                            msg, symbol_raw=symbol_raw, market_type=self.market_type
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)
                continue
