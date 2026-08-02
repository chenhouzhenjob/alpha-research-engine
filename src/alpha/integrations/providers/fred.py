"""FRED 宏观序列适配器（不实现 VenueAdapter）。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from alpha.collection import RawArchive
from alpha.schema import Instrument, MacroPoint, make_instrument_id


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _date_to_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def resolve_api_key() -> str:
    """从环境变量读取 FRED_API_KEY。"""
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "缺少 FRED 密钥：请在仓库根目录 .env 中设置 FRED_API_KEY（可参考 .env.example）"
        )
    return key


def parse_observations_payload(
    data: dict[str, Any],
    *,
    series_id: str,
    frequency: str,
    unit: str | None = None,
    market_type: str = "macro",
) -> list[MacroPoint]:
    """解析 FRED series/observations JSON 为 MacroPoint。"""
    ingest = _now_ms()
    inst = make_instrument_id("fred", market_type, series_id)
    out: list[MacroPoint] = []
    for row in data.get("observations") or []:
        if not isinstance(row, dict):
            continue
        date_s = row.get("date")
        raw_val = row.get("value")
        if not date_s or raw_val is None or str(raw_val).strip() in {"", "."}:
            continue
        try:
            value = float(raw_val)
        except (TypeError, ValueError):
            continue
        ts = _date_to_ms(str(date_s))
        out.append(
            MacroPoint(
                venue="fred",
                market_type=market_type,
                instrument_id=inst,
                symbol_raw=series_id,
                ts_event_ms=ts,
                ts_ingest_ms=ingest,
                source_seq=str(date_s),
                metric="value",
                value=value,
                unit=unit,
                frequency=frequency,
            )
        )
    return out


class FredAdapter:
    """FRED 宏观观测序列（market_type=macro）。"""

    venue = "fred"

    def __init__(
        self,
        *,
        api_key: str,
        market_type: str = "macro",
        rest_base: str = "https://api.stlouisfed.org/fred",
        series: dict[str, dict[str, Any]] | None = None,
        raw: RawArchive | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.market_type = market_type
        self.rest_base = rest_base.rstrip("/")
        self.series = series or {}
        self.raw = raw
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=0.5, min=0.5, max=8))
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        q = dict(params or {})
        q["api_key"] = self._api_key
        q.setdefault("file_type", "json")
        url = f"{self.rest_base}/{path.lstrip('/')}"
        resp = await self._client.get(url, params=q)
        if resp.status_code == 429:
            raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
        resp.raise_for_status()
        data = resp.json()
        if self.raw is not None:
            endpoint = path.strip("/").replace("/", "_")
            safe_params = {k: v for k, v in q.items() if k != "api_key"}
            self.raw.append(self.venue, endpoint, {"params": safe_params, "data": data})
        return data

    def series_ids(self) -> list[str]:
        return list(self.series.keys())

    def series_meta(self, series_id: str) -> dict[str, Any]:
        return dict(self.series.get(series_id) or {})

    async def list_instruments(self) -> list[Instrument]:
        """按配置 series 注册 fred:macro:{SERIES_ID}。"""
        if not self.series:
            raise SystemExit("fred.yaml 需配置 series 映射")
        out: list[Instrument] = []
        for sid, meta in self.series.items():
            out.append(
                Instrument(
                    instrument_id=make_instrument_id(self.venue, self.market_type, sid),
                    venue=self.venue,
                    market_type=self.market_type,
                    base=sid,
                    quote="USD",
                    settle=None,
                    symbol_raw=sid,
                    meta_json=json.dumps(
                        {
                            "frequency": meta.get("frequency"),
                            "unit": meta.get("unit"),
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        return out

    async def fetch_macro(
        self,
        series_id: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[MacroPoint]:
        """拉取 series/observations，可选 observation_start/end。"""
        meta = self.series_meta(series_id)
        frequency = str(meta.get("frequency") or "unknown")
        unit = meta.get("unit")
        params: dict[str, Any] = {
            "series_id": series_id,
            "sort_order": "asc",
        }
        if start_ms is not None:
            params["observation_start"] = _ms_to_date(start_ms)
        if end_ms is not None:
            # FRED end 为含当日；须端用前一天边界近似
            params["observation_end"] = _ms_to_date(max(start_ms or 0, end_ms - 1))
        data = await self._get("series/observations", params)
        if not isinstance(data, dict):
            return []
        points = parse_observations_payload(
            data,
            series_id=series_id,
            frequency=frequency,
            unit=str(unit) if unit is not None else None,
            market_type=self.market_type,
        )
        if start_ms is not None:
            points = [p for p in points if p.ts_event_ms >= start_ms]
        if end_ms is not None:
            points = [p for p in points if p.ts_event_ms < end_ms]
        return points
