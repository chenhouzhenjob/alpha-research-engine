"""AkShare A 股 ETF：日线 OHLCV、日终 NAV、实时价+官方 IOPV（可选依赖）。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Callable

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from alpha.collection import RawArchive
from alpha.schema import (
    EtfIopvPoint,
    FundingRate,
    Instrument,
    OhlcvBar,
    TradeTick,
    make_instrument_id,
)

SOURCE_NAV_EOD = "nav_eod"
SOURCE_IOPV_REALTIME = "iopv_realtime"


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _date_to_utc_ms(d: date | datetime | str) -> int:
    """日历日 → 该日 UTC 00:00 毫秒。"""
    if isinstance(d, datetime):
        d = d.date()
    elif isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d").date()
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_yyyymmdd(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%d")


def _require_akshare() -> Any:
    try:
        import akshare as ak
    except ImportError as exc:
        raise ImportError(
            "需要安装 AkShare：pip install -e \".[cn]\" 或 pip install akshare"
        ) from exc
    return ak


def compute_premium(price: float | None, iopv: float) -> float | None:
    if price is None or iopv <= 0:
        return None
    return (float(price) - float(iopv)) / float(iopv)


def parse_ohlcv_hist_df(
    df: pd.DataFrame,
    *,
    symbol_raw: str,
    tf: str,
    venue: str,
    market_type: str,
    start_ms: int,
    end_ms: int,
) -> list[OhlcvBar]:
    """解析 fund_etf_hist_em 风格 DataFrame。"""
    if df is None or df.empty:
        return []
    out: list[OhlcvBar] = []
    ingest = _now_ms()
    for _, row in df.iterrows():
        day = row.get("日期")
        if day is None or (isinstance(day, float) and pd.isna(day)):
            continue
        ts = _date_to_utc_ms(day)
        if ts < start_ms or ts >= end_ms:
            continue
        try:
            o, h, l, c = float(row["开盘"]), float(row["最高"]), float(row["最低"]), float(row["收盘"])
            vol = float(row.get("成交量") or 0.0)
        except (TypeError, ValueError, KeyError):
            continue
        if not (o > 0 and h > 0 and l > 0 and c > 0):
            continue
        qv = row.get("成交额")
        quote_volume = float(qv) if qv is not None and not pd.isna(qv) else None
        out.append(
            OhlcvBar(
                venue=venue,
                market_type=market_type,
                instrument_id=make_instrument_id(venue, market_type, symbol_raw),
                symbol_raw=symbol_raw,
                ts_event_ms=ts,
                ts_ingest_ms=ingest,
                source_seq=str(day)[:10],
                tf=tf,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=vol,
                quote_volume=quote_volume,
            )
        )
    out.sort(key=lambda b: b.ts_event_ms)
    return out


def parse_nav_eod_df(
    nav_df: pd.DataFrame,
    *,
    symbol_raw: str,
    venue: str,
    market_type: str,
    start_ms: int,
    end_ms: int,
    close_by_ts: dict[int, float] | None = None,
) -> list[EtfIopvPoint]:
    """解析 fund_etf_fund_info_em 风格净值表；可选按日对齐收盘价。"""
    if nav_df is None or nav_df.empty:
        return []
    close_by_ts = close_by_ts or {}
    out: list[EtfIopvPoint] = []
    ingest = _now_ms()
    for _, row in nav_df.iterrows():
        day = row.get("净值日期")
        if day is None or (isinstance(day, float) and pd.isna(day)):
            continue
        ts = _date_to_utc_ms(day)
        if ts < start_ms or ts >= end_ms:
            continue
        try:
            iopv = float(row["单位净值"])
        except (TypeError, ValueError, KeyError):
            continue
        if iopv <= 0:
            continue
        price = close_by_ts.get(ts)
        out.append(
            EtfIopvPoint(
                venue=venue,
                market_type=market_type,
                instrument_id=make_instrument_id(venue, market_type, symbol_raw),
                symbol_raw=symbol_raw,
                ts_event_ms=ts,
                ts_ingest_ms=ingest,
                source_seq=f"nav:{str(day)[:10]}",
                iopv=iopv,
                price=price,
                premium=compute_premium(price, iopv),
                source=SOURCE_NAV_EOD,
            )
        )
    out.sort(key=lambda p: p.ts_event_ms)
    return out


def parse_spot_iopv_df(
    df: pd.DataFrame,
    *,
    symbols: list[str],
    venue: str,
    market_type: str,
    ts_event_ms: int | None = None,
) -> list[EtfIopvPoint]:
    """解析 fund_etf_spot_em 全表，过滤 symbols。"""
    if df is None or df.empty:
        return []
    want = {str(s).zfill(6) for s in symbols}
    code_col = "代码" if "代码" in df.columns else None
    if code_col is None:
        return []
    price_col = "最新价" if "最新价" in df.columns else None
    iopv_col = "IOPV实时估值" if "IOPV实时估值" in df.columns else None
    if price_col is None or iopv_col is None:
        return []
    ts = ts_event_ms if ts_event_ms is not None else _now_ms()
    ingest = _now_ms()
    out: list[EtfIopvPoint] = []
    for _, row in df.iterrows():
        code = str(row[code_col]).split(".")[0].strip().zfill(6)
        if code not in want:
            continue
        try:
            iopv = float(row[iopv_col])
            price = float(row[price_col])
        except (TypeError, ValueError):
            continue
        if iopv <= 0 or price <= 0:
            continue
        out.append(
            EtfIopvPoint(
                venue=venue,
                market_type=market_type,
                instrument_id=make_instrument_id(venue, market_type, code),
                symbol_raw=code,
                ts_event_ms=ts,
                ts_ingest_ms=ingest,
                source_seq=f"spot:{code}:{ts}",
                iopv=iopv,
                price=price,
                premium=compute_premium(price, iopv),
                source=SOURCE_IOPV_REALTIME,
            )
        )
    return out


class AkshareEtfProvider:
    """AkShare ETF 适配器；实现 VenueAdapter 行情面（funding/stream 为空）。"""

    venue: str = "akshare"
    market_type: str = "etf"

    def __init__(
        self,
        *,
        symbols: list[str] | None = None,
        market_type: str = "etf",
        request_interval_sec: float = 0.5,
        adjust: str = "",
        raw: RawArchive | None = None,
        ak_module: Any | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.market_type = market_type
        self.symbols = [str(s).zfill(6) for s in (symbols or [])]
        self.request_interval_sec = float(request_interval_sec)
        self.adjust = adjust
        self.raw = raw
        self._ak = ak_module
        self._sleep = sleep_fn or time.sleep
        self._last_call_mono = 0.0

    def _akshare(self) -> Any:
        if self._ak is not None:
            return self._ak
        self._ak = _require_akshare()
        return self._ak

    def _throttle(self) -> None:
        gap = self.request_interval_sec
        if gap <= 0:
            return
        elapsed = time.monotonic() - self._last_call_mono
        if elapsed < gap:
            self._sleep(gap - elapsed)
        self._last_call_mono = time.monotonic()

    def _archive(self, endpoint: str, payload: object) -> None:
        if self.raw is not None:
            self.raw.append(self.venue, endpoint, payload)

    def _call_with_retry(self, fn: Callable[[], Any]) -> Any:
        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)
        def _inner() -> Any:
            self._throttle()
            return fn()

        return _inner()

    async def list_instruments(self) -> list[Instrument]:
        if not self.symbols:
            raise SystemExit("akshare.yaml 需配置 symbols 列表")
        out: list[Instrument] = []
        for sym in self.symbols:
            out.append(
                Instrument(
                    instrument_id=make_instrument_id(self.venue, self.market_type, sym),
                    venue=self.venue,
                    market_type=self.market_type,
                    base=sym,
                    quote="CNY",
                    settle="CNY",
                    symbol_raw=sym,
                    meta_json=json.dumps({"provider": "akshare"}, ensure_ascii=False),
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
        if tf != "1d":
            raise ValueError(f"AkShare ETF 一期仅支持 tf=1d，收到: {tf}")
        if start_ms >= end_ms:
            return []
        symbol = str(symbol_raw).zfill(6)

        def _call() -> pd.DataFrame:
            ak = self._akshare()
            df = ak.fund_etf_hist_em(
                symbol=symbol,
                period="daily",
                start_date=_ms_to_yyyymmdd(start_ms),
                end_date=_ms_to_yyyymmdd(end_ms - 1),
                adjust=self.adjust,
            )
            self._archive(
                "fund_etf_hist_em",
                {"symbol": symbol, "rows": len(df) if df is not None else 0},
            )
            return df

        df = await asyncio.to_thread(self._call_with_retry, _call)
        return parse_ohlcv_hist_df(
            df,
            symbol_raw=symbol,
            tf=tf,
            venue=self.venue,
            market_type=self.market_type,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    async def fetch_nav_eod(
        self,
        symbol_raw: str,
        start_ms: int,
        end_ms: int,
    ) -> list[EtfIopvPoint]:
        if start_ms >= end_ms:
            return []
        symbol = str(symbol_raw).zfill(6)

        def _call_nav() -> pd.DataFrame:
            ak = self._akshare()
            df = ak.fund_etf_fund_info_em(
                fund=symbol,
                start_date=_ms_to_yyyymmdd(start_ms),
                end_date=_ms_to_yyyymmdd(end_ms - 1),
            )
            self._archive(
                "fund_etf_fund_info_em",
                {"fund": symbol, "rows": len(df) if df is not None else 0},
            )
            return df

        def _call_hist() -> pd.DataFrame:
            ak = self._akshare()
            df = ak.fund_etf_hist_em(
                symbol=symbol,
                period="daily",
                start_date=_ms_to_yyyymmdd(start_ms),
                end_date=_ms_to_yyyymmdd(end_ms - 1),
                adjust=self.adjust,
            )
            self._archive(
                "fund_etf_hist_em",
                {"symbol": symbol, "for": "nav_price_join", "rows": len(df) if df is not None else 0},
            )
            return df

        nav_df = await asyncio.to_thread(self._call_with_retry, _call_nav)
        try:
            hist_df = await asyncio.to_thread(self._call_with_retry, _call_hist)
        except Exception:
            hist_df = None
        close_by_ts: dict[int, float] = {}
        if hist_df is not None and not hist_df.empty:
            for _, row in hist_df.iterrows():
                day = row.get("日期")
                if day is None or (isinstance(day, float) and pd.isna(day)):
                    continue
                try:
                    close_by_ts[_date_to_utc_ms(day)] = float(row["收盘"])
                except (TypeError, ValueError, KeyError):
                    continue
        return parse_nav_eod_df(
            nav_df,
            symbol_raw=symbol,
            venue=self.venue,
            market_type=self.market_type,
            start_ms=start_ms,
            end_ms=end_ms,
            close_by_ts=close_by_ts,
        )

    async def fetch_iopv_realtime(
        self,
        symbols: list[str] | None = None,
    ) -> list[EtfIopvPoint]:
        syms = [str(s).zfill(6) for s in (symbols if symbols is not None else self.symbols)]
        if not syms:
            return []

        def _call() -> pd.DataFrame:
            ak = self._akshare()
            df = ak.fund_etf_spot_em()
            self._archive(
                "fund_etf_spot_em",
                {"rows": len(df) if df is not None else 0, "filter": syms},
            )
            return df

        df = await asyncio.to_thread(self._call_with_retry, _call)
        return parse_spot_iopv_df(
            df,
            symbols=syms,
            venue=self.venue,
            market_type=self.market_type,
        )

    async def fetch_funding(
        self,
        symbol_raw: str,
        start_ms: int,
        end_ms: int,
    ) -> list[FundingRate]:
        _ = (symbol_raw, start_ms, end_ms)
        return []

    async def stream_trades(self, symbol_raw: str) -> AsyncIterator[TradeTick]:
        _ = symbol_raw
        return
        yield  # pragma: no cover

    async def close(self) -> None:
        return
