"""DuckDB 查询辅助：研究侧读取 canonical Parquet。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from alpha.collection.storage.canonical import glob_canonical


def _connect(data_dir: str | Path) -> duckdb.DuckDBPyConnection:
    # 仅查询，不落盘 duckdb 文件
    con = duckdb.connect(database=":memory:")
    # 注册方便路径
    con.execute(f"SET home_directory='{Path(data_dir).resolve()}'")
    return con


def load_ohlcv(
    data_dir: str | Path,
    *,
    venue: str | None = None,
    market_type: str | None = None,
    symbol_raw: str | None = None,
    tf: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """加载 OHLCV 为字典列表（UTC ms）。"""
    files = glob_canonical(data_dir, "ohlcv")
    if not files:
        return []
    # 用 glob 读入
    pattern = str(Path(data_dir).resolve() / "canonical" / "ohlcv" / "**" / "*.parquet")
    con = _connect(data_dir)
    where = ["1=1"]
    params: list[Any] = []
    if venue:
        where.append("venue = ?")
        params.append(venue)
    if market_type:
        where.append("market_type = ?")
        params.append(market_type)
    if symbol_raw:
        where.append("symbol_raw = ?")
        params.append(symbol_raw)
    if tf:
        where.append("tf = ?")
        params.append(tf)
    if start_ms is not None:
        where.append("ts_event_ms >= ?")
        params.append(start_ms)
    if end_ms is not None:
        where.append("ts_event_ms < ?")
        params.append(end_ms)
    sql = f"""
    SELECT * FROM read_parquet('{pattern}', hive_partitioning=1)
    WHERE {' AND '.join(where)}
    ORDER BY venue, symbol_raw, ts_event_ms
    """
    try:
        cur = con.execute(sql, params)
    except duckdb.Error:
        # hive 失败时退回显式文件列表
        file_list = ", ".join(f"'{p}'" for p in files)
        sql = f"""
        SELECT * FROM read_parquet([{file_list}])
        WHERE {' AND '.join(where)}
        ORDER BY venue, symbol_raw, ts_event_ms
        """
        cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_funding(
    data_dir: str | Path,
    *,
    venue: str | None = None,
    market_type: str | None = None,
    symbol_raw: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """加载资金费率。"""
    files = glob_canonical(data_dir, "funding")
    if not files:
        return []
    file_list = ", ".join(f"'{p}'" for p in files)
    where = ["1=1"]
    params: list[Any] = []
    if venue:
        where.append("venue = ?")
        params.append(venue)
    if market_type:
        where.append("market_type = ?")
        params.append(market_type)
    if symbol_raw:
        where.append("symbol_raw = ?")
        params.append(symbol_raw)
    if start_ms is not None:
        where.append("ts_event_ms >= ?")
        params.append(start_ms)
    if end_ms is not None:
        where.append("ts_event_ms < ?")
        params.append(end_ms)
    con = _connect(data_dir)
    sql = f"""
    SELECT * FROM read_parquet([{file_list}])
    WHERE {' AND '.join(where)}
    ORDER BY venue, symbol_raw, ts_event_ms
    """
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_metric_dataset(
    data_dir: str | Path,
    dataset: str,
    *,
    venue: str | None = None,
    market_type: str | None = None,
    symbol_raw: str | None = None,
    metric: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """按单一 dataset 目录加载长表指标（fundamental 或 macro）。"""
    files = glob_canonical(data_dir, dataset)
    if not files:
        return []
    file_list = ", ".join(f"'{p}'" for p in files)
    where = ["1=1"]
    params: list[Any] = []
    if venue:
        where.append("venue = ?")
        params.append(venue)
    if market_type:
        where.append("market_type = ?")
        params.append(market_type)
    if symbol_raw:
        where.append("symbol_raw = ?")
        params.append(symbol_raw)
    if metric:
        where.append("metric = ?")
        params.append(metric)
    if start_ms is not None:
        where.append("ts_event_ms >= ?")
        params.append(start_ms)
    if end_ms is not None:
        where.append("ts_event_ms < ?")
        params.append(end_ms)
    con = _connect(data_dir)
    sql = f"""
    SELECT * FROM read_parquet([{file_list}])
    WHERE {' AND '.join(where)}
    ORDER BY venue, symbol_raw, metric, ts_event_ms
    """
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_fundamental(
    data_dir: str | Path,
    *,
    venue: str | None = None,
    market_type: str | None = None,
    symbol_raw: str | None = None,
    metric: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """加载公司基本面（仅扫描 canonical/fundamental）。"""
    return _load_metric_dataset(
        data_dir,
        "fundamental",
        venue=venue,
        market_type=market_type,
        symbol_raw=symbol_raw,
        metric=metric,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def load_macro(
    data_dir: str | Path,
    *,
    venue: str | None = None,
    market_type: str | None = None,
    symbol_raw: str | None = None,
    metric: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """加载宏观指标（仅扫描 canonical/macro）。"""
    return _load_metric_dataset(
        data_dir,
        "macro",
        venue=venue,
        market_type=market_type,
        symbol_raw=symbol_raw,
        metric=metric,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def _load_tick_dataset(
    data_dir: str | Path,
    dataset: str,
    *,
    venue: str | None = None,
    market_type: str | None = None,
    symbol_raw: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """加载 trade / quote 等 tick 类数据集。"""
    files = glob_canonical(data_dir, dataset)
    if not files:
        return []
    file_list = ", ".join(f"'{p}'" for p in files)
    where = ["1=1"]
    params: list[Any] = []
    if venue:
        where.append("venue = ?")
        params.append(venue)
    if market_type:
        where.append("market_type = ?")
        params.append(market_type)
    if symbol_raw:
        where.append("symbol_raw = ?")
        params.append(symbol_raw)
    if start_ms is not None:
        where.append("ts_event_ms >= ?")
        params.append(start_ms)
    if end_ms is not None:
        where.append("ts_event_ms < ?")
        params.append(end_ms)
    con = _connect(data_dir)
    sql = f"""
    SELECT * FROM read_parquet([{file_list}])
    WHERE {' AND '.join(where)}
    ORDER BY venue, symbol_raw, ts_event_ms
    """
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_trades(
    data_dir: str | Path,
    *,
    venue: str | None = None,
    market_type: str | None = None,
    symbol_raw: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """加载成交 tick（仅扫描 canonical/trade）。"""
    return _load_tick_dataset(
        data_dir,
        "trade",
        venue=venue,
        market_type=market_type,
        symbol_raw=symbol_raw,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def load_quote(
    data_dir: str | Path,
    *,
    venue: str | None = None,
    market_type: str | None = None,
    symbol_raw: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """加载 L1 报价（仅扫描 canonical/quote）。"""
    return _load_tick_dataset(
        data_dir,
        "quote",
        venue=venue,
        market_type=market_type,
        symbol_raw=symbol_raw,
        start_ms=start_ms,
        end_ms=end_ms,
    )
