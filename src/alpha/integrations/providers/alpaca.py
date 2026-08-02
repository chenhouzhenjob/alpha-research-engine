"""Alpaca 美股原生 REST 适配器：OHLCV / 历史 trades / quotes（不经官方 SDK）。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from alpha.collection import RawArchive
from alpha.schema import (
    FundingRate,
    Instrument,
    OhlcvBar,
    QuoteTick,
    TradeTick,
    make_instrument_id,
)

# 项目 tf → Alpaca timeframe
_TF_MAP = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "1h": "1Hour",
    "1d": "1Day",
}


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _ms_to_rfc3339(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts_ms(t: Any) -> int:
    """Alpaca 时间字段 → UTC 毫秒（纳秒/微秒截断到 ms）。"""
    if isinstance(t, (int, float)):
        ts = int(t)
        # 纳秒
        if ts >= 10_000_000_000_000_000:
            return ts // 1_000_000
        # 微秒
        if ts >= 10_000_000_000_000:
            return ts // 1_000
        # 秒
        if ts < 10_000_000_000:
            return ts * 1000
        return ts
    dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def parse_bar(row: dict[str, Any], *, symbol_raw: str, tf: str, market_type: str) -> OhlcvBar:
    """解析 Alpaca bar 对象为 OhlcvBar。ts_event_ms 取 bar 起始时间 t。"""
    t = row["t"]
    ts_ms = _parse_ts_ms(t)
    n = row.get("n")
    return OhlcvBar(
        venue="alpaca",
        market_type=market_type,
        instrument_id=make_instrument_id("alpaca", market_type, symbol_raw),
        symbol_raw=symbol_raw,
        ts_event_ms=ts_ms,
        ts_ingest_ms=_now_ms(),
        source_seq=str(t),
        tf=tf,
        open=float(row["o"]),
        high=float(row["h"]),
        low=float(row["l"]),
        close=float(row["c"]),
        volume=float(row["v"]),
        trade_count=int(n) if n is not None else None,
    )


def parse_trade_hist(
    row: dict[str, Any],
    *,
    symbol_raw: str,
    market_type: str,
    feed: str,
) -> TradeTick:
    """解析 Alpaca 历史 trade 对象。"""
    t = row["t"]
    ts_ms = _parse_ts_ms(t)
    trade_id = str(row["i"])
    return TradeTick(
        venue="alpaca",
        market_type=market_type,
        instrument_id=make_instrument_id("alpaca", market_type, symbol_raw),
        symbol_raw=symbol_raw,
        ts_event_ms=ts_ms,
        ts_ingest_ms=_now_ms(),
        source_seq=trade_id,
        price=float(row["p"]),
        size=float(row["s"]),
        side="unknown",
        trade_id=trade_id,
        feed=feed,
    )


def parse_quote(
    row: dict[str, Any],
    *,
    symbol_raw: str,
    market_type: str,
    feed: str,
) -> QuoteTick:
    """解析 Alpaca L1 quote 对象。"""
    t = row["t"]
    ts_ms = _parse_ts_ms(t)
    bp = float(row["bp"])
    bs = float(row["bs"])
    ap = float(row["ap"])
    az = float(row["as"])
    return QuoteTick(
        venue="alpaca",
        market_type=market_type,
        instrument_id=make_instrument_id("alpaca", market_type, symbol_raw),
        symbol_raw=symbol_raw,
        ts_event_ms=ts_ms,
        ts_ingest_ms=_now_ms(),
        source_seq=f"{t}|{bp}|{bs}|{ap}|{az}",
        bid_px=bp,
        bid_sz=bs,
        ask_px=ap,
        ask_sz=az,
        feed=feed,
    )


def resolve_credentials() -> tuple[str, str]:
    """从环境变量读取密钥（由仓库根目录 .env 经 load_dotenv 注入）。"""
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_API_SECRET", "").strip()
    if not key or not secret:
        raise SystemExit(
            "缺少 Alpaca 密钥：请在仓库根目录 .env 中设置 ALPACA_API_KEY / ALPACA_API_SECRET"
            "（可参考 .env.example）"
        )
    return key, secret


class AlpacaAdapter:
    """Alpaca 美股 bars / 历史 trades / quotes（market_type=stock）。"""

    venue = "alpaca"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        market_type: str = "stock",
        data_base: str = "https://data.alpaca.markets",
        trade_base: str = "https://paper-api.alpaca.markets",
        symbols: list[str] | None = None,
        feed: str = "sip",
        adjustment: str = "split",
        raw: RawArchive | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.market_type = market_type
        self.data_base = data_base.rstrip("/")
        self.trade_base = trade_base.rstrip("/")
        self.symbols = symbols or []
        self.feed = feed
        self.adjustment = adjustment
        self.raw = raw
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }
        self._client = httpx.AsyncClient(headers=self._headers, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    def _effective_end_ms(self, end_ms: int) -> int:
        """免费 SIP 近 15 分钟受限；sip 时将 end 截到 now-16m。"""
        if self.feed == "sip":
            cap = _now_ms() - 16 * 60_000
            return min(end_ms, cap)
        return end_ms

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=0.5, min=0.5, max=8))
    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET；429 时退避重试。"""
        resp = await self._client.get(url, params=params)
        if resp.status_code == 429:
            raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
        resp.raise_for_status()
        data = resp.json()
        if self.raw is not None:
            endpoint = url.replace(self.data_base, "").replace(self.trade_base, "")
            endpoint = endpoint.strip("/").replace("/", "_") or "alpaca"
            self.raw.append(
                self.venue,
                endpoint,
                {"params": params, "data": data},
            )
        return data

    async def list_instruments(self) -> list[Instrument]:
        """
        以配置 symbols 为准生成 Instrument。
        若配置了 symbols，可选请求 assets 校验；失败则仍用配置列表。
        """
        symbols = list(self.symbols)
        meta_by_sym: dict[str, dict[str, Any]] = {}
        if symbols:
            try:
                # Trading API：校验存在性（参数名 us_equity 为 Alpaca 官方字段）
                data = await self._get(
                    f"{self.trade_base}/v2/assets",
                    {"status": "active", "asset_class": "us_equity"},
                )
                if isinstance(data, list):
                    want = set(symbols)
                    for a in data:
                        sym = a.get("symbol")
                        if sym in want:
                            meta_by_sym[sym] = a
            except Exception:
                meta_by_sym = {}
        else:
            # 未配置则拉一批 active equity（慎用，体量大）；要求显式配置
            raise SystemExit("alpaca.yaml 需配置 symbols 列表")

        out: list[Instrument] = []
        for sym in symbols:
            meta = meta_by_sym.get(sym, {})
            out.append(
                Instrument(
                    instrument_id=make_instrument_id(self.venue, self.market_type, sym),
                    venue=self.venue,
                    market_type=self.market_type,
                    base=sym,
                    quote="USD",
                    settle="USD",
                    symbol_raw=sym,
                    meta_json=json.dumps(
                        {
                            "name": meta.get("name"),
                            "exchange": meta.get("exchange"),
                            "feed": self.feed,
                            "adjustment": self.adjustment,
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
        """分页拉取 /v2/stocks/{symbol}/bars。"""
        if tf not in _TF_MAP:
            raise ValueError(f"Alpaca 不支持的 timeframe: {tf}，可选 {sorted(_TF_MAP)}")

        effective_end = self._effective_end_ms(end_ms)
        if start_ms >= effective_end:
            return []

        bars: list[OhlcvBar] = []
        page_token: str | None = None
        url = f"{self.data_base}/v2/stocks/{symbol_raw}/bars"
        while True:
            params: dict[str, Any] = {
                "timeframe": _TF_MAP[tf],
                "start": _ms_to_rfc3339(start_ms),
                "end": _ms_to_rfc3339(effective_end),
                "limit": 10000,
                "adjustment": self.adjustment,
                "feed": self.feed,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            data = await self._get(url, params)
            rows = data.get("bars") or []
            for row in rows:
                bar = parse_bar(
                    row, symbol_raw=symbol_raw, tf=tf, market_type=self.market_type
                )
                if start_ms <= bar.ts_event_ms < end_ms:
                    bars.append(bar)
            page_token = data.get("next_page_token")
            if not page_token:
                break
            await asyncio.sleep(0.05)
        return bars

    def _log_page_progress(
        self,
        *,
        kind: str,
        symbol_raw: str,
        page: int,
        total: int,
        last_ts_ms: int | None,
        t0: float,
        force: bool = False,
    ) -> None:
        """每 10 页或强制打印一条进度（少量日志）。"""
        if not force and page % 10 != 0:
            return
        elapsed = time.perf_counter() - t0
        last = _ms_to_rfc3339(last_ts_ms) if last_ts_ms is not None else "-"
        print(
            f"[alpaca] {symbol_raw} {kind}: 第 {page} 页, 累计 {total} 条, "
            f"最新事件 {last}, 已用 {elapsed:.1f}s",
            flush=True,
        )

    async def fetch_trades_hist(
        self,
        symbol_raw: str,
        start_ms: int,
        end_ms: int,
    ) -> list[TradeTick]:
        """分页拉取 /v2/stocks/{symbol}/trades。"""
        effective_end = self._effective_end_ms(end_ms)
        if start_ms >= effective_end:
            return []

        ticks: list[TradeTick] = []
        page_token: str | None = None
        page = 0
        t0 = time.perf_counter()
        url = f"{self.data_base}/v2/stocks/{symbol_raw}/trades"
        while True:
            params: dict[str, Any] = {
                "start": _ms_to_rfc3339(start_ms),
                "end": _ms_to_rfc3339(effective_end),
                "limit": 10000,
                "feed": self.feed,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            data = await self._get(url, params)
            rows = data.get("trades") or []
            page += 1
            for row in rows:
                tick = parse_trade_hist(
                    row,
                    symbol_raw=symbol_raw,
                    market_type=self.market_type,
                    feed=self.feed,
                )
                if start_ms <= tick.ts_event_ms < end_ms:
                    ticks.append(tick)
            last_ts = ticks[-1].ts_event_ms if ticks else None
            page_token = data.get("next_page_token")
            self._log_page_progress(
                kind="trade",
                symbol_raw=symbol_raw,
                page=page,
                total=len(ticks),
                last_ts_ms=last_ts,
                t0=t0,
                force=not page_token,
            )
            if not page_token:
                break
            await asyncio.sleep(0.05)
        return ticks

    async def fetch_quotes(
        self,
        symbol_raw: str,
        start_ms: int,
        end_ms: int,
    ) -> list[QuoteTick]:
        """分页拉取 /v2/stocks/{symbol}/quotes。"""
        effective_end = self._effective_end_ms(end_ms)
        if start_ms >= effective_end:
            return []

        quotes: list[QuoteTick] = []
        page_token: str | None = None
        page = 0
        t0 = time.perf_counter()
        url = f"{self.data_base}/v2/stocks/{symbol_raw}/quotes"
        while True:
            params: dict[str, Any] = {
                "start": _ms_to_rfc3339(start_ms),
                "end": _ms_to_rfc3339(effective_end),
                "limit": 10000,
                "feed": self.feed,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            data = await self._get(url, params)
            rows = data.get("quotes") or []
            page += 1
            for row in rows:
                q = parse_quote(
                    row,
                    symbol_raw=symbol_raw,
                    market_type=self.market_type,
                    feed=self.feed,
                )
                if start_ms <= q.ts_event_ms < end_ms:
                    quotes.append(q)
            last_ts = quotes[-1].ts_event_ms if quotes else None
            page_token = data.get("next_page_token")
            self._log_page_progress(
                kind="quote",
                symbol_raw=symbol_raw,
                page=page,
                total=len(quotes),
                last_ts_ms=last_ts,
                t0=t0,
                force=not page_token,
            )
            if not page_token:
                break
            await asyncio.sleep(0.05)
        return quotes

    async def fetch_funding(
        self,
        symbol_raw: str,
        start_ms: int,
        end_ms: int,
    ) -> list[FundingRate]:
        """股票无资金费率。"""
        return []

    async def stream_trades(self, symbol_raw: str) -> AsyncIterator[TradeTick]:
        """本阶段不接实时成交流（历史用 fetch_trades_hist）。"""
        _ = symbol_raw
        return
        yield  # pragma: no cover — 使本函数成为异步生成器
