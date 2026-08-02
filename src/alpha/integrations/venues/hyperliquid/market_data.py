"""Hyperliquid Info REST / WS 原生适配器（不经 CCXT）。"""

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

# Hyperliquid 支持的 interval
_TF_SET = {
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
}


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def parse_candle(row: dict[str, Any], *, market_type: str) -> OhlcvBar:
    """解析 candleSnapshot 单根。ts_event_ms 使用开盘 t。"""
    symbol_raw = str(row["s"])
    return OhlcvBar(
        venue="hyperliquid",
        market_type=market_type,
        instrument_id=make_instrument_id("hyperliquid", market_type, symbol_raw),
        symbol_raw=symbol_raw,
        ts_event_ms=int(row["t"]),
        ts_ingest_ms=_now_ms(),
        source_seq=str(row["t"]),
        tf=str(row["i"]),
        open=float(row["o"]),
        high=float(row["h"]),
        low=float(row["l"]),
        close=float(row["c"]),
        volume=float(row["v"]),
        trade_count=int(row["n"]) if row.get("n") is not None else None,
    )


def parse_funding_hist(row: dict[str, Any], *, symbol_raw: str, market_type: str) -> FundingRate:
    """解析 fundingHistory 单条。"""
    # 字段: coin, fundingRate, premium, time
    return FundingRate(
        venue="hyperliquid",
        market_type=market_type,
        instrument_id=make_instrument_id("hyperliquid", market_type, symbol_raw),
        symbol_raw=symbol_raw,
        ts_event_ms=int(row["time"]),
        ts_ingest_ms=_now_ms(),
        source_seq=str(row["time"]),
        funding_rate=float(row["fundingRate"]),
    )


def parse_trade(msg: dict[str, Any], *, symbol_raw: str, market_type: str) -> list[TradeTick]:
    """解析 WS trades 频道推送（可能为数组）。"""
    # 订阅后 data 形如 { channel, data: [ { coin, side, px, sz, time, hash, tid }, ... ] }
    data = msg.get("data", msg)
    trades = data if isinstance(data, list) else [data]
    out: list[TradeTick] = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        side_raw = str(t.get("side", "")).lower()
        if side_raw in ("b", "buy"):
            side = "buy"
        elif side_raw in ("a", "sell", "s"):
            side = "sell"
        else:
            side = "unknown"
        tid = str(t.get("tid") or t.get("hash") or f"{t.get('time')}-{t.get('px')}-{t.get('sz')}")
        out.append(
            TradeTick(
                venue="hyperliquid",
                market_type=market_type,
                instrument_id=make_instrument_id("hyperliquid", market_type, symbol_raw),
                symbol_raw=symbol_raw,
                ts_event_ms=int(t["time"]),
                ts_ingest_ms=_now_ms(),
                source_seq=tid,
                price=float(t["px"]),
                size=float(t["sz"]),
                side=side,  # type: ignore[arg-type]
                trade_id=tid,
            )
        )
    return out


class HyperliquidAdapter:
    """Hyperliquid 永续。"""

    venue = "hyperliquid"

    def __init__(
        self,
        *,
        market_type: str = "perp",
        info_url: str = "https://api.hyperliquid.xyz/info",
        ws_url: str = "wss://api.hyperliquid.xyz/ws",
        symbols: list[str] | None = None,
        raw: RawArchive | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.market_type = market_type
        self.info_url = info_url
        self.ws_url = ws_url
        self.symbols = symbols or []
        self.raw = raw
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=0.5, min=0.5, max=8))
    async def _info(self, body: dict[str, Any]) -> Any:
        """POST /info。"""
        resp = await self._client.post(self.info_url, json=body)
        if resp.status_code == 429:
            raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
        resp.raise_for_status()
        data = resp.json()
        if self.raw is not None:
            self.raw.append(
                self.venue,
                f"info_{body.get('type', 'unknown')}",
                {"body": body, "data": data},
            )
        return data

    async def list_instruments(self) -> list[Instrument]:
        """metaAndAssetCtxs → universe。"""
        data = await self._info({"type": "metaAndAssetCtxs"})
        # [meta, assetCtxs]
        meta = data[0] if isinstance(data, list) else data
        universe = meta.get("universe", [])
        out: list[Instrument] = []
        want = set(self.symbols) if self.symbols else None
        for u in universe:
            name = u.get("name")
            if not name:
                continue
            if want is not None and name not in want:
                continue
            if u.get("isDelisted"):
                continue
            out.append(
                Instrument(
                    instrument_id=make_instrument_id(self.venue, self.market_type, name),
                    venue=self.venue,
                    market_type=self.market_type,
                    base=name,
                    quote="USD",
                    settle="USDC",
                    symbol_raw=name,
                    meta_json=json.dumps(
                        {
                            "szDecimals": u.get("szDecimals"),
                            "maxLeverage": u.get("maxLeverage"),
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
        """
        candleSnapshot。官方仅保留最近约 5000 根；
        超窗时仍按请求区间拉取，由服务端截断。
        """
        if tf not in _TF_SET:
            raise ValueError(f"不支持的 timeframe: {tf}")
        data = await self._info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol_raw,
                    "interval": tf,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )
        bars: list[OhlcvBar] = []
        if not isinstance(data, list):
            return bars
        for row in data:
            bar = parse_candle(row, market_type=self.market_type)
            if start_ms <= bar.ts_event_ms < end_ms:
                bars.append(bar)
        bars.sort(key=lambda b: b.ts_event_ms)
        return bars

    async def fetch_funding(
        self,
        symbol_raw: str,
        start_ms: int,
        end_ms: int,
    ) -> list[FundingRate]:
        """fundingHistory。"""
        data = await self._info(
            {
                "type": "fundingHistory",
                "coin": symbol_raw,
                "startTime": start_ms,
                "endTime": end_ms,
            }
        )
        out: list[FundingRate] = []
        if not isinstance(data, list):
            return out
        for row in data:
            fr = parse_funding_hist(row, symbol_raw=symbol_raw, market_type=self.market_type)
            if start_ms <= fr.ts_event_ms < end_ms:
                out.append(fr)

        # 从 metaAndAssetCtxs 补充当前 funding（可选）
        try:
            meta_ctx = await self._info({"type": "metaAndAssetCtxs"})
            meta, ctxs = meta_ctx[0], meta_ctx[1]
            names = [u["name"] for u in meta.get("universe", [])]
            if symbol_raw in names:
                idx = names.index(symbol_raw)
                ctx = ctxs[idx]
                # funding 字段为当前小时费率字符串
                rate = ctx.get("funding")
                mark = ctx.get("markPx")
                oracle = ctx.get("oraclePx")
                if rate is not None and end_ms >= _now_ms() - 60_000:
                    out.append(
                        FundingRate(
                            venue=self.venue,
                            market_type=self.market_type,
                            instrument_id=make_instrument_id(
                                self.venue, self.market_type, symbol_raw
                            ),
                            symbol_raw=symbol_raw,
                            ts_event_ms=_now_ms(),
                            ts_ingest_ms=_now_ms(),
                            source_seq="live",
                            funding_rate=float(rate),
                            mark_price=float(mark) if mark is not None else None,
                            index_price=float(oracle) if oracle is not None else None,
                        )
                    )
        except Exception:
            pass

        out.sort(key=lambda x: x.ts_event_ms)
        return out

    async def stream_trades(self, symbol_raw: str) -> AsyncIterator[TradeTick]:
        """订阅 trades 频道，断线重连。"""
        sub = {
            "method": "subscribe",
            "subscription": {"type": "trades", "coin": symbol_raw},
        }
        while True:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=60
                ) as ws:
                    await ws.send(json.dumps(sub))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if self.raw is not None:
                            self.raw.append(self.venue, "ws_trades", msg)
                        # 忽略 subscriptionResponse
                        if msg.get("channel") == "subscriptionResponse":
                            continue
                        if msg.get("channel") != "trades" and "data" not in msg:
                            continue
                        for tick in parse_trade(
                            msg, symbol_raw=symbol_raw, market_type=self.market_type
                        ):
                            yield tick
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)
                continue
