"""Finnhub 美股公司基本面适配器（不实现 VenueAdapter）。"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from alpha.collection import RawArchive
from alpha.schema import FundamentalPoint, Instrument, make_instrument_id


def _retryable_http(exc: BaseException) -> bool:
    """仅对限流/网络抖动重试；403/401（无权限）不重试。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))

# 关键指标白名单（/stock/metric 的 metric 字典字段）
_METRIC_KEYS = {
    "peBasicExclExtraTTM": ("pe_ttm", "ratio"),
    "peTTM": ("pe_ttm", "ratio"),
    "pbAnnual": ("pb", "ratio"),
    "pbQuarterly": ("pb", "ratio"),
    "roeTTM": ("roe_ttm", "ratio"),
    "roaTTM": ("roa_ttm", "ratio"),
    "currentRatioAnnual": ("current_ratio", "ratio"),
    "currentRatioQuarterly": ("current_ratio", "ratio"),
    "marketCapitalization": ("market_cap", "USD"),
    "epsBasicExclExtraItemsTTM": ("eps_ttm", "USD"),
    "epsTTM": ("eps_ttm", "USD"),
}

_SKIP_FINANCIAL_KEYS = frozenset({"period", "year", "quarter", "form", "filedDate", "acceptedDate"})


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _date_to_ms(date_str: str) -> int:
    """YYYY-MM-DD → UTC 日起点毫秒。"""
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s or s in {".", "None", "null", "N/A"}:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def resolve_api_key() -> str:
    """从环境变量读取 FINNHUB_API_KEY。"""
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "缺少 Finnhub 密钥：请在仓库根目录 .env 中设置 FINNHUB_API_KEY（可参考 .env.example）"
        )
    return key


def parse_financials_payload(
    data: dict[str, Any],
    *,
    symbol_raw: str,
    statement: str,
    frequency: str,
    market_type: str = "stock",
) -> list[FundamentalPoint]:
    """解析 /stock/financials 响应为 FundamentalPoint 列表。"""
    ingest = _now_ms()
    inst = make_instrument_id("finnhub", market_type, symbol_raw)
    out: list[FundamentalPoint] = []
    rows = data.get("financials") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        period = row.get("period")
        if not period:
            continue
        ts = _date_to_ms(str(period))
        for key, raw_val in row.items():
            if key in _SKIP_FINANCIAL_KEYS:
                continue
            val = _to_float(raw_val)
            if val is None:
                continue
            out.append(
                FundamentalPoint(
                    venue="finnhub",
                    market_type=market_type,
                    instrument_id=inst,
                    symbol_raw=symbol_raw,
                    ts_event_ms=ts,
                    ts_ingest_ms=ingest,
                    source_seq=f"{statement}:{frequency}:{period}:{key}",
                    metric=str(key),
                    value=val,
                    unit="USD",
                    frequency=frequency,
                    statement=statement,
                )
            )
    return out


def parse_metric_payload(
    data: dict[str, Any],
    *,
    symbol_raw: str,
    market_type: str = "stock",
    as_of_ms: int | None = None,
) -> list[FundamentalPoint]:
    """解析 /stock/metric 快照（白名单字段）为 FundamentalPoint。"""
    ingest = _now_ms()
    ts = as_of_ms if as_of_ms is not None else _date_to_ms(
        datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    )
    inst = make_instrument_id("finnhub", market_type, symbol_raw)
    metric_obj = data.get("metric") or {}
    out: list[FundamentalPoint] = []
    seen: set[str] = set()
    for src_key, (canon, unit) in _METRIC_KEYS.items():
        if canon in seen:
            continue
        val = _to_float(metric_obj.get(src_key))
        if val is None:
            continue
        seen.add(canon)
        out.append(
            FundamentalPoint(
                venue="finnhub",
                market_type=market_type,
                instrument_id=inst,
                symbol_raw=symbol_raw,
                ts_event_ms=ts,
                ts_ingest_ms=ingest,
                source_seq=f"metric:{src_key}",
                metric=canon,
                value=val,
                unit=unit,
                frequency="ttm",
                statement="metric",
            )
        )
    return out


def parse_earnings_payload(
    data: list[Any] | dict[str, Any],
    *,
    symbol_raw: str,
    market_type: str = "stock",
) -> list[FundamentalPoint]:
    """解析 /stock/earnings 为 FundamentalPoint。"""
    ingest = _now_ms()
    inst = make_instrument_id("finnhub", market_type, symbol_raw)
    rows = data if isinstance(data, list) else (data.get("earnings") or data.get("data") or [])
    out: list[FundamentalPoint] = []
    field_map = (
        ("actual", "eps_actual", "USD"),
        ("estimate", "eps_estimate", "USD"),
        ("surprise", "eps_surprise", "USD"),
        ("surprisePercent", "eps_surprise_percent", "percent"),
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        period = row.get("period")
        if not period:
            continue
        ts = _date_to_ms(str(period))
        for src, metric, unit in field_map:
            val = _to_float(row.get(src))
            if val is None:
                continue
            out.append(
                FundamentalPoint(
                    venue="finnhub",
                    market_type=market_type,
                    instrument_id=inst,
                    symbol_raw=symbol_raw,
                    ts_event_ms=ts,
                    ts_ingest_ms=ingest,
                    source_seq=f"earnings:{period}:{src}",
                    metric=metric,
                    value=val,
                    unit=unit,
                    frequency="quarterly",
                    statement="earnings",
                )
            )
    return out


class FinnhubAdapter:
    """Finnhub 公司基本面（market_type=stock）。"""

    venue = "finnhub"

    def __init__(
        self,
        *,
        api_key: str,
        market_type: str = "stock",
        rest_base: str = "https://finnhub.io/api/v1",
        symbols: list[str] | None = None,
        statements: list[str] | None = None,
        frequencies: list[str] | None = None,
        fetch_financials: bool = False,
        fetch_metric: bool = True,
        fetch_earnings: bool = True,
        max_requests_per_minute: int = 50,
        raw: RawArchive | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.market_type = market_type
        self.rest_base = rest_base.rstrip("/")
        self.symbols = symbols or []
        self.statements = statements or ["ic", "bs", "cf"]
        self.frequencies = frequencies or ["annual", "quarterly"]
        # 免费档通常无 /stock/financials；默认关，付费再开
        self.fetch_financials = fetch_financials
        self.fetch_metric = fetch_metric
        self.fetch_earnings = fetch_earnings
        self.raw = raw
        self._api_key = api_key
        self._min_interval = 60.0 / max(1, max_requests_per_minute)
        self._last_request_ts = 0.0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_ts = time.monotonic()

    @retry(
        retry=retry_if_exception(_retryable_http),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._throttle()
        q = dict(params or {})
        q["token"] = self._api_key
        url = f"{self.rest_base}/{path.lstrip('/')}"
        resp = await self._client.get(url, params=q)
        if resp.status_code == 429:
            raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
        if resp.status_code in (401, 403):
            # 避免把 token 打进 traceback URL
            body = (resp.text or "")[:200]
            raise SystemExit(
                f"Finnhub 拒绝访问 {path}（HTTP {resp.status_code}）。"
                f"免费档通常不含标准化报表 /stock/financials；"
                f"请在 finnhub.yaml 保持 fetch_financials: false，"
                f"仅用 fetch_metric / fetch_earnings。"
                f" 响应摘要: {body}"
            )
        resp.raise_for_status()
        data = resp.json()
        if self.raw is not None:
            endpoint = path.strip("/").replace("/", "_")
            # 不把 token 写入 raw
            safe_params = {k: v for k, v in q.items() if k != "token"}
            self.raw.append(self.venue, endpoint, {"params": safe_params, "data": data})
        return data

    async def list_instruments(self) -> list[Instrument]:
        """按配置 symbols 注册 finnhub:stock:{SYM}。"""
        if not self.symbols:
            raise SystemExit("finnhub.yaml 需配置 symbols 列表")
        out: list[Instrument] = []
        for sym in self.symbols:
            out.append(
                Instrument(
                    instrument_id=make_instrument_id(self.venue, self.market_type, sym),
                    venue=self.venue,
                    market_type=self.market_type,
                    base=sym,
                    quote="USD",
                    settle="USD",
                    symbol_raw=sym,
                    meta_json="{}",
                )
            )
        return out

    async def fetch_fundamentals(
        self,
        symbol_raw: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[FundamentalPoint]:
        """拉取可选报表 + metric/earnings，并按时间窗过滤。"""
        points: list[FundamentalPoint] = []
        if self.fetch_financials:
            for statement in self.statements:
                for freq in self.frequencies:
                    data = await self._get(
                        "stock/financials",
                        {"symbol": symbol_raw, "statement": statement, "freq": freq},
                    )
                    if isinstance(data, dict):
                        points.extend(
                            parse_financials_payload(
                                data,
                                symbol_raw=symbol_raw,
                                statement=statement,
                                frequency=freq,
                                market_type=self.market_type,
                            )
                        )
        if self.fetch_metric:
            data = await self._get("stock/metric", {"symbol": symbol_raw, "metric": "all"})
            if isinstance(data, dict):
                points.extend(
                    parse_metric_payload(
                        data, symbol_raw=symbol_raw, market_type=self.market_type
                    )
                )
        if self.fetch_earnings:
            data = await self._get("stock/earnings", {"symbol": symbol_raw})
            points.extend(
                parse_earnings_payload(
                    data, symbol_raw=symbol_raw, market_type=self.market_type
                )
            )

        if start_ms is not None:
            points = [p for p in points if p.ts_event_ms >= start_ms]
        if end_ms is not None:
            points = [p for p in points if p.ts_event_ms < end_ms]
        return points
