"""Raw 归档与 Canonical Parquet 分区写入。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from alpha.schema import (
    Envelope,
    FundamentalPoint,
    FundingRate,
    MacroPoint,
    OhlcvBar,
    QuoteTick,
    TradeTick,
)


def _utc_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


class RawArchive:
    """按 venue/endpoint/date 追加 JSONL。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "raw"

    def append(self, venue: str, endpoint: str, payload: object, ts_ms: int | None = None) -> Path:
        """写入一行 raw JSON，返回文件路径。"""
        ts = ts_ms if ts_ms is not None else _now_ms()
        day = _utc_date(ts)
        path = self.root / venue / endpoint / f"date={day}" / "part.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts_ingest_ms": ts, "payload": payload}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return path


class CanonicalStore:
    """Canonical Parquet：按 dataset/venue/(tf)/date 分区，写入前按业务键去重。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "canonical"

    def _partition_dir(self, row: Envelope) -> Path:
        day = _utc_date(row.ts_event_ms)
        parts = [self.root, row.dataset, f"venue={row.venue}"]
        if isinstance(row, OhlcvBar):
            parts.append(f"tf={row.tf}")
        parts.append(f"date={day}")
        return Path(*parts)

    def write_ohlcv(self, rows: Sequence[OhlcvBar]) -> int:
        """写入 OHLCV，按 (instrument_id, ts_event_ms, tf) 去重。"""
        return self._write_grouped(
            rows,
            key_fn=lambda r: (r.instrument_id, r.ts_event_ms, r.tf),
            schema=_ohlcv_arrow_schema(),
            to_dict=_ohlcv_to_dict,
        )

    def write_trades(self, rows: Sequence[TradeTick]) -> int:
        """写入成交，按 (instrument_id, trade_id) 去重。"""
        return self._write_grouped(
            rows,
            key_fn=lambda r: (r.instrument_id, r.trade_id),
            schema=_trade_arrow_schema(),
            to_dict=_trade_to_dict,
        )

    def write_funding(self, rows: Sequence[FundingRate]) -> int:
        """写入资金费率，按 (instrument_id, ts_event_ms) 去重。"""
        return self._write_grouped(
            rows,
            key_fn=lambda r: (r.instrument_id, r.ts_event_ms),
            schema=_funding_arrow_schema(),
            to_dict=_funding_to_dict,
        )

    def write_fundamental(self, rows: Sequence[FundamentalPoint]) -> int:
        """写入公司基本面，按 (instrument_id, metric, statement, frequency, ts_event_ms) 去重。"""
        return self._write_grouped(
            rows,
            key_fn=lambda r: (
                r.instrument_id,
                r.metric,
                r.statement,
                r.frequency,
                r.ts_event_ms,
            ),
            schema=_fundamental_arrow_schema(),
            to_dict=_fundamental_to_dict,
        )

    def write_macro(self, rows: Sequence[MacroPoint]) -> int:
        """写入宏观指标，按 (instrument_id, metric, frequency, ts_event_ms) 去重。"""
        return self._write_grouped(
            rows,
            key_fn=lambda r: (r.instrument_id, r.metric, r.frequency, r.ts_event_ms),
            schema=_macro_arrow_schema(),
            to_dict=_macro_to_dict,
        )

    def write_quotes(self, rows: Sequence[QuoteTick]) -> int:
        """写入 L1 报价，按 (instrument_id, ts_event_ms, bid/ask, feed) 去重。"""
        return self._write_grouped(
            rows,
            key_fn=lambda r: (
                r.instrument_id,
                r.ts_event_ms,
                r.bid_px,
                r.ask_px,
                r.bid_sz,
                r.ask_sz,
                r.feed,
            ),
            schema=_quote_arrow_schema(),
            to_dict=_quote_to_dict,
        )

    def _write_grouped(
        self,
        rows: Sequence[Envelope],
        key_fn,
        schema: pa.Schema,
        to_dict,
    ) -> int:
        if not rows:
            return 0
        # 按分区聚合
        buckets: dict[Path, list[Envelope]] = {}
        for r in rows:
            buckets.setdefault(self._partition_dir(r), []).append(r)

        written = 0
        for part_dir, items in buckets.items():
            part_dir.mkdir(parents=True, exist_ok=True)
            path = part_dir / "part-000.parquet"
            existing = _read_table(path, schema)
            new_table = pa.Table.from_pylist([to_dict(r) for r in items], schema=schema)
            merged = _merge_dedupe(existing, new_table, key_fn_name=_key_cols_for(items[0]))
            pq.write_table(merged, path)
            written += len(items)
        return written


def _key_cols_for(row: Envelope) -> list[str]:
    if isinstance(row, OhlcvBar):
        return ["instrument_id", "ts_event_ms", "tf"]
    if isinstance(row, TradeTick):
        return ["instrument_id", "trade_id"]
    if isinstance(row, QuoteTick):
        return [
            "instrument_id",
            "ts_event_ms",
            "bid_px",
            "ask_px",
            "bid_sz",
            "ask_sz",
            "feed",
        ]
    if isinstance(row, FundingRate):
        return ["instrument_id", "ts_event_ms"]
    if isinstance(row, FundamentalPoint):
        return ["instrument_id", "metric", "statement", "frequency", "ts_event_ms"]
    if isinstance(row, MacroPoint):
        return ["instrument_id", "metric", "frequency", "ts_event_ms"]
    return ["instrument_id", "ts_event_ms"]


def _read_table(path: Path, schema: pa.Schema) -> pa.Table | None:
    if not path.exists():
        return None
    return pq.read_table(path)


def _align_table(table: pa.Table, schema: pa.Schema) -> pa.Table:
    """按目标 schema 对齐列（缺列补 null，便于旧 parquet 加字段）。"""
    arrays = []
    for field in schema:
        if field.name in table.column_names:
            arrays.append(table.column(field.name).cast(field.type, safe=False))
        else:
            arrays.append(pa.nulls(table.num_rows, type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def _merge_dedupe(
    existing: pa.Table | None,
    new_table: pa.Table,
    key_fn_name: list[str],
) -> pa.Table:
    """合并后按关键键保留最后一条（新覆盖旧）。"""
    if existing is None or existing.num_rows == 0:
        base = new_table
    else:
        existing = _align_table(existing, new_table.schema)
        base = pa.concat_tables([existing, new_table])

    # 用 pandas 式去重较重；这里用字典保留最后出现
    rows = base.to_pylist()
    seen: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(row[c] for c in key_fn_name)
        seen[key] = row
    merged_rows = list(seen.values())
    # 按时间排序便于阅读
    merged_rows.sort(key=lambda r: (r.get("ts_event_ms") or 0, r.get("instrument_id") or ""))
    return pa.Table.from_pylist(merged_rows, schema=new_table.schema)


def _ohlcv_to_dict(r: OhlcvBar) -> dict:
    return r.model_dump()


def _trade_to_dict(r: TradeTick) -> dict:
    return r.model_dump()


def _funding_to_dict(r: FundingRate) -> dict:
    return r.model_dump()


def _fundamental_to_dict(r: FundamentalPoint) -> dict:
    return r.model_dump()


def _macro_to_dict(r: MacroPoint) -> dict:
    return r.model_dump()


def _quote_to_dict(r: QuoteTick) -> dict:
    return r.model_dump()


def _ohlcv_arrow_schema() -> pa.Schema:
    return pa.schema(
        [
            ("schema_version", pa.int32()),
            ("dataset", pa.string()),
            ("venue", pa.string()),
            ("market_type", pa.string()),
            ("instrument_id", pa.string()),
            ("symbol_raw", pa.string()),
            ("ts_event_ms", pa.int64()),
            ("ts_ingest_ms", pa.int64()),
            ("source_seq", pa.string()),
            ("tf", pa.string()),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("volume", pa.float64()),
            ("trade_count", pa.int64()),
            ("quote_volume", pa.float64()),
        ]
    )


def _trade_arrow_schema() -> pa.Schema:
    return pa.schema(
        [
            ("schema_version", pa.int32()),
            ("dataset", pa.string()),
            ("venue", pa.string()),
            ("market_type", pa.string()),
            ("instrument_id", pa.string()),
            ("symbol_raw", pa.string()),
            ("ts_event_ms", pa.int64()),
            ("ts_ingest_ms", pa.int64()),
            ("source_seq", pa.string()),
            ("price", pa.float64()),
            ("size", pa.float64()),
            ("side", pa.string()),
            ("trade_id", pa.string()),
            ("is_buyer_maker", pa.bool_()),
            ("feed", pa.string()),
        ]
    )


def _quote_arrow_schema() -> pa.Schema:
    return pa.schema(
        [
            ("schema_version", pa.int32()),
            ("dataset", pa.string()),
            ("venue", pa.string()),
            ("market_type", pa.string()),
            ("instrument_id", pa.string()),
            ("symbol_raw", pa.string()),
            ("ts_event_ms", pa.int64()),
            ("ts_ingest_ms", pa.int64()),
            ("source_seq", pa.string()),
            ("bid_px", pa.float64()),
            ("bid_sz", pa.float64()),
            ("ask_px", pa.float64()),
            ("ask_sz", pa.float64()),
            ("feed", pa.string()),
        ]
    )


def _funding_arrow_schema() -> pa.Schema:
    return pa.schema(
        [
            ("schema_version", pa.int32()),
            ("dataset", pa.string()),
            ("venue", pa.string()),
            ("market_type", pa.string()),
            ("instrument_id", pa.string()),
            ("symbol_raw", pa.string()),
            ("ts_event_ms", pa.int64()),
            ("ts_ingest_ms", pa.int64()),
            ("source_seq", pa.string()),
            ("funding_rate", pa.float64()),
            ("mark_price", pa.float64()),
            ("index_price", pa.float64()),
            ("next_funding_ts_ms", pa.int64()),
        ]
    )


def _fundamental_arrow_schema() -> pa.Schema:
    return pa.schema(
        [
            ("schema_version", pa.int32()),
            ("dataset", pa.string()),
            ("venue", pa.string()),
            ("market_type", pa.string()),
            ("instrument_id", pa.string()),
            ("symbol_raw", pa.string()),
            ("ts_event_ms", pa.int64()),
            ("ts_ingest_ms", pa.int64()),
            ("source_seq", pa.string()),
            ("metric", pa.string()),
            ("value", pa.float64()),
            ("unit", pa.string()),
            ("frequency", pa.string()),
            ("statement", pa.string()),
        ]
    )


def _macro_arrow_schema() -> pa.Schema:
    return pa.schema(
        [
            ("schema_version", pa.int32()),
            ("dataset", pa.string()),
            ("venue", pa.string()),
            ("market_type", pa.string()),
            ("instrument_id", pa.string()),
            ("symbol_raw", pa.string()),
            ("ts_event_ms", pa.int64()),
            ("ts_ingest_ms", pa.int64()),
            ("source_seq", pa.string()),
            ("metric", pa.string()),
            ("value", pa.float64()),
            ("unit", pa.string()),
            ("frequency", pa.string()),
        ]
    )


def glob_canonical(data_dir: str | Path, dataset: str) -> list[Path]:
    """列出某 dataset 下所有 parquet 文件。"""
    root = Path(data_dir) / "canonical" / dataset
    if not root.exists():
        return []
    return sorted(root.rglob("*.parquet"))
