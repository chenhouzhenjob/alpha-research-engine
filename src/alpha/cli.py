"""CLI 入口：backfill / sync / stream / catalog。"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from alpha.backtest import BacktestConfig, run_backtest
from alpha.collection import (
    CanonicalStore,
    Catalog,
    RawArchive,
    load_fundamental,
    load_funding,
    load_macro,
    load_ohlcv,
    load_quote,
    load_trades,
)
from alpha.config import default_root, load_collect, load_source, load_yaml
from alpha.features import FeatureSet, ReturnFeature, SmaFeature, compute_features, write_features
from alpha.integrations import (
    AlpacaAdapter,
    FinnhubAdapter,
    FredAdapter,
    VenueAdapter,
    create_market_data_adapter,
)
from alpha.integrations.providers.alpaca import resolve_credentials
from alpha.integrations.providers.finnhub import resolve_api_key as resolve_finnhub_key
from alpha.integrations.providers.fred import resolve_api_key as resolve_fred_key
from alpha.strategies import MovingAverageCrossStrategy

# 行情 VenueAdapter；基本面/宏观走独立适配器
_MARKET_SOURCES = frozenset({"binance_perp", "binance_spot", "hyperliquid", "alpaca"})
_FUNDAMENTAL_SOURCES = frozenset({"finnhub"})
_MACRO_SOURCES = frozenset({"fred"})
_ALL_SOURCES = _MARKET_SOURCES | _FUNDAMENTAL_SOURCES | _MACRO_SOURCES


def _research_features(cfg: dict[str, Any]) -> FeatureSet:
    items = []
    for spec in cfg.get("features", []):
        kind = spec["type"]
        if kind == "sma":
            items.append(SmaFeature(window=int(spec["window"])))
        elif kind == "return":
            items.append(ReturnFeature(periods=int(spec.get("periods", 1))))
        else:
            raise SystemExit(f"未知 feature: {kind}")
    return FeatureSet(name=str(cfg["feature_set"]), features=tuple(items))


def _research_inputs(cfg: dict[str, Any], data_dir: Path) -> dict[str, pd.DataFrame]:
    """按研究 YAML 声明加载任意 canonical 数据集；兼容旧的单一 data 配置。"""
    input_specs = cfg.get("inputs") or {"ohlcv": cfg["data"]}
    if not isinstance(input_specs, dict):
        raise SystemExit("research inputs 必须是 mapping")
    loaders = {
        "ohlcv": load_ohlcv,
        "funding": load_funding,
        "fundamental": load_fundamental,
        "macro": load_macro,
        "trade": load_trades,
        "quote": load_quote,
    }
    inputs: dict[str, pd.DataFrame] = {}
    for dataset, spec in input_specs.items():
        if dataset not in loaders:
            raise SystemExit(f"不支持的 research 输入数据集: {dataset}")
        if not isinstance(spec, dict):
            raise SystemExit(f"research inputs.{dataset} 必须是 mapping")
        kwargs: dict[str, Any] = {
            key: spec[key]
            for key in ("venue", "market_type", "symbol_raw", "start_ms", "end_ms")
            if key in spec
        }
        if dataset == "ohlcv" and "tf" in spec:
            kwargs["tf"] = spec["tf"]
        if dataset in {"fundamental", "macro"} and "metric" in spec:
            kwargs["metric"] = spec["metric"]
        rows = loaders[dataset](data_dir, **kwargs)
        inputs[dataset] = pd.DataFrame(rows)
    return inputs


def cmd_feature_run(args: argparse.Namespace) -> int:
    cfg = load_collect(args.config)
    research = load_yaml(args.research_config)
    data_dir = _resolve_data_dir(cfg, args.data_dir)
    features = compute_features(_research_inputs(research, data_dir), _research_features(research))
    path = write_features(data_dir, str(research["feature_set"]), features)
    print(f"feature 写入 {len(features)} 行: {path}")
    return 0


def cmd_backtest_run(args: argparse.Namespace) -> int:
    cfg = load_collect(args.config)
    research = load_yaml(args.research_config)
    data_dir = _resolve_data_dir(cfg, args.data_dir)
    inputs = _research_inputs(research, data_dir)
    if "ohlcv" not in inputs or inputs["ohlcv"].empty:
        raise SystemExit("bar 级回测需要非空的 ohlcv 输入")
    bars = inputs["ohlcv"]
    feature_set = _research_features(research)
    features = compute_features(inputs, feature_set)
    strategy_cfg = research.get("strategy", {})
    if strategy_cfg.get("type", "moving_average_cross") != "moving_average_cross":
        raise SystemExit("首期仅支持 moving_average_cross")
    strategy = MovingAverageCrossStrategy(
        fast_feature=str(strategy_cfg.get("fast_feature", "sma_10")),
        slow_feature=str(strategy_cfg.get("slow_feature", "sma_20")),
        long_only=bool(strategy_cfg.get("long_only", True)),
    )
    bt = research.get("backtest", {})
    result = run_backtest(bars, strategy.target_weights(bars, features), BacktestConfig(
        initial_cash=float(bt.get("initial_cash", 100_000)), fee_bps=float(bt.get("fee_bps", 0)),
        slippage_bps=float(bt.get("slippage_bps", 0)),
        max_gross_leverage=float(bt.get("max_gross_leverage", 1)),
    ))
    root = result.write(data_dir, str(research.get("run_id", "latest")))
    print(f"backtest 完成: {root}")
    return 0


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _parse_venues(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _resolve_sources(
    collect: dict[str, Any], args: argparse.Namespace, *, default_sources: list[str] | None = None
) -> list[str]:
    """从用户的 canonical venue/market_type 筛选内部 source profiles。"""
    venues = set(_parse_venues(args.venue)) if args.venue else None
    configured = list(collect.get("sources") or [])
    if venues is not None and default_sources is None:
        candidates = configured + sorted(_ALL_SOURCES - set(configured))
    else:
        candidates = default_sources if default_sources is not None else configured
    selected: list[str] = []
    for source in candidates:
        cfg = load_source(source)
        if venues is not None and cfg.get("venue") not in venues:
            continue
        if args.market_type is not None and cfg.get("market_type") != args.market_type:
            continue
        selected.append(source)
    if venues is not None and not selected:
        raise SystemExit(f"没有匹配的 source: venue={args.venue}, market_type={args.market_type}")
    return selected


def _parse_utc_bound(s: str) -> int:
    """
    解析 UTC 边界为毫秒。
    支持 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS[Z]。
    仅日期时取当日 00:00:00 UTC（半开区间上界：--end 2024-02-01 = 不含 2 月 1 日）。
    """
    raw = s.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    norm = raw.replace("Z", "+00:00")
    if "T" not in norm and " " in norm:
        norm = norm.replace(" ", "T", 1)
    dt = datetime.fromisoformat(norm)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return int(dt.timestamp() * 1000)


def _resolve_time_window(
    args: argparse.Namespace,
    *,
    default_days: int | None = 1,
    allow_open_start: bool = False,
) -> tuple[int | None, int]:
    """
    时间窗 [start_ms, end_ms)（UTC）。
    优先 --start/--end；否则 --days；再否则 default_days。
    allow_open_start=True 且无 start/days/default 时 start_ms=None（全量起点，仅水位裁剪）。
    """
    start_arg = getattr(args, "start", None)
    end_arg = getattr(args, "end", None)
    days_arg = getattr(args, "days", None)

    if start_arg and days_arg is not None:
        raise SystemExit("请勿同时使用 --start 与 --days，二选一")

    if end_arg:
        end_ms = _parse_utc_bound(end_arg)
    else:
        end_ms = _now_ms()

    if start_arg:
        start_ms: int | None = _parse_utc_bound(start_arg)
    elif days_arg is not None:
        start_ms = end_ms - int(days_arg) * 86_400_000
    elif default_days is not None:
        start_ms = end_ms - int(default_days) * 86_400_000
    elif allow_open_start:
        start_ms = None
    else:
        raise SystemExit("请指定 --start/--end 或 --days")

    if start_ms is not None and start_ms >= end_ms:
        raise SystemExit(f"无效时间窗: start_ms={start_ms} >= end_ms={end_ms}")
    return start_ms, end_ms


def _fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_time_window_args(parser: argparse.ArgumentParser, *, days_help: str) -> None:
    """为子命令添加时间窗与水位控制参数。"""
    parser.add_argument(
        "--start",
        default=None,
        help="UTC 起始 YYYY-MM-DD 或带时刻；与 --days 二选一",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="UTC 结束（半开 [start,end)）；日期为当日 00:00；默认现在",
    )
    parser.add_argument("--days", type=int, default=None, help=days_help)
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略本次读取到的 watermark，按指定时间窗重拉；成功后仍更新 watermark",
    )


def _add_source_filter_args(parser: argparse.ArgumentParser) -> None:
    """按 canonical venue 筛选内部 source profile。"""
    parser.add_argument("--venue", default=None, help="逗号分隔平台标识，如 binance,alpaca")
    parser.add_argument(
        "--market-type", default=None, help="可选市场类型，如 spot、perp、stock 或 macro"
    )


# 兼容旧名
def _resolve_tick_window(args: argparse.Namespace) -> tuple[int, int]:
    start_ms, end_ms = _resolve_time_window(args, default_days=1, allow_open_start=False)
    assert start_ms is not None
    return start_ms, end_ms


def _build_adapter(source: str, cfg: dict[str, Any], raw: RawArchive | None) -> VenueAdapter:
    if source == "binance_perp":
        return create_market_data_adapter("binance",
            market_type=cfg.get("market_type", "perp"),
            rest_base=cfg.get("rest_base", "https://fapi.binance.com"),
            ws_base=cfg.get("ws_base", "wss://fstream.binance.com"),
            symbols=list(cfg.get("symbols") or []),
            raw=raw,
        )
    if source == "binance_spot":
        return create_market_data_adapter("binance_spot",
            market_type=cfg.get("market_type", "spot"),
            rest_base=cfg.get("rest_base", "https://api.binance.com"),
            ws_base=cfg.get("ws_base", "wss://stream.binance.com:9443"),
            symbols=list(cfg.get("symbols") or []),
            raw=raw,
        )
    if source == "hyperliquid":
        return create_market_data_adapter("hyperliquid",
            market_type=cfg.get("market_type", "perp"),
            info_url=cfg.get("info_url", "https://api.hyperliquid.xyz/info"),
            ws_url=cfg.get("ws_url", "wss://api.hyperliquid.xyz/ws"),
            symbols=list(cfg.get("symbols") or []),
            raw=raw,
        )
    if source == "alpaca":
        key, secret = resolve_credentials()
        return create_market_data_adapter("alpaca",
            api_key=key,
            api_secret=secret,
            market_type=cfg.get("market_type", "stock"),
            data_base=cfg.get("data_base", "https://data.alpaca.markets"),
            trade_base=cfg.get("trade_base", "https://paper-api.alpaca.markets"),
            symbols=list(cfg.get("symbols") or []),
            feed=cfg.get("feed", "sip"),
            adjustment=cfg.get("adjustment", "split"),
            raw=raw,
        )
    raise SystemExit(f"未知行情 source: {source}")


def _instrument_id(source: str, cfg: dict[str, Any], symbol_raw: str, default_market_type: str) -> str:
    """Watermark 使用 source 名，canonical instrument 使用交易所真实 venue 名。"""
    canonical_venue = str(cfg.get("venue", source))
    market_type = str(cfg.get("market_type", default_market_type))
    return f"{canonical_venue}:{market_type}:{symbol_raw}"


def _build_finnhub(cfg: dict[str, Any], raw: RawArchive | None) -> FinnhubAdapter:
    return FinnhubAdapter(
        api_key=resolve_finnhub_key(),
        market_type=cfg.get("market_type", "stock"),
        rest_base=cfg.get("rest_base", "https://finnhub.io/api/v1"),
        symbols=list(cfg.get("symbols") or []),
        statements=list(cfg.get("statements") or ["ic", "bs", "cf"]),
        frequencies=list(cfg.get("frequencies") or ["annual", "quarterly"]),
        fetch_financials=bool(cfg.get("fetch_financials", False)),
        fetch_metric=bool(cfg.get("fetch_metric", True)),
        fetch_earnings=bool(cfg.get("fetch_earnings", True)),
        max_requests_per_minute=int(cfg.get("max_requests_per_minute", 50)),
        raw=raw,
    )


def _build_fred(cfg: dict[str, Any], raw: RawArchive | None) -> FredAdapter:
    series = cfg.get("series") or {}
    if not isinstance(series, dict):
        raise SystemExit("fred.yaml 的 series 须为 mapping")
    return FredAdapter(
        api_key=resolve_fred_key(),
        market_type=cfg.get("market_type", "macro"),
        rest_base=cfg.get("rest_base", "https://api.stlouisfed.org/fred"),
        series=series,
        raw=raw,
    )


def _resolve_data_dir(collect: dict[str, Any], override: str | None) -> Path:
    if override:
        return Path(override)
    d = collect.get("data_dir", "data")
    p = Path(d)
    if not p.is_absolute():
        p = default_root() / p
    return p


def _resolve_catalog(collect: dict[str, Any], data_dir: Path, override: str | None) -> Path:
    if override:
        return Path(override)
    c = collect.get("catalog_path")
    if c:
        p = Path(c)
        return p if p.is_absolute() else default_root() / p
    return data_dir / "catalog.sqlite"


async def cmd_refresh_instruments(args: argparse.Namespace) -> int:
    """从各 source 拉取可交易标的，写入 catalog.instruments。"""
    collect = load_collect(args.config)
    data_dir = _resolve_data_dir(collect, args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = _resolve_catalog(collect, data_dir, args.catalog)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    raw = RawArchive(data_dir)
    catalog = Catalog(str(catalog_path))
    await catalog.open()
    sources = _resolve_sources(collect, args)
    total = 0
    try:
        for v in sources:
            vcfg = load_source(v)
            if v in _FUNDAMENTAL_SOURCES:
                adapter = _build_finnhub(vcfg, raw)
            elif v in _MACRO_SOURCES:
                adapter = _build_fred(vcfg, raw)
            elif v in _MARKET_SOURCES:
                adapter = _build_adapter(v, vcfg, raw)
            else:
                raise SystemExit(f"未知 source: {v}")
            try:
                instruments = await adapter.list_instruments()
                n = await catalog.upsert_instruments(instruments)
                total += n
                print(f"[{v}] 注册 {n} 个标的")
            finally:
                await adapter.close()
    finally:
        await catalog.close()
    print(f"完成，共 {total} 条")
    return 0


async def cmd_backfill_ohlcv(args: argparse.Namespace) -> int:
    """按水位增量回填 OHLCV，写入 canonical Parquet 并推进 watermark。"""
    collect = load_collect(args.config)
    data_dir = _resolve_data_dir(collect, args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = _resolve_catalog(collect, data_dir, args.catalog)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    raw = RawArchive(data_dir)
    store = CanonicalStore(data_dir)
    catalog = Catalog(str(catalog_path))
    await catalog.open()

    start_ms, end_ms = _resolve_time_window(
        args, default_days=int(collect.get("backfill_days", 7))
    )
    assert start_ms is not None
    print(
        f"[ohlcv] 时间窗 [{_fmt_ms(start_ms)}, {_fmt_ms(end_ms)})（水位之后增量）",
        flush=True,
    )

    sources = _resolve_sources(collect, args)

    try:
        for v in sources:
            if v not in _MARKET_SOURCES:
                print(f"[{v}] 跳过 ohlcv（非行情 source）")
                continue
            vcfg = load_source(v)
            # 周期：CLI --tf > source.timeframes > collect.timeframes
            if args.tf:
                tfs = [args.tf]
            elif vcfg.get("timeframes"):
                tfs = list(vcfg["timeframes"])
            else:
                tfs = list(collect.get("timeframes") or ["1h"])
            adapter = _build_adapter(v, vcfg, raw)
            symbols = list(vcfg.get("symbols") or [])
            try:
                for sym in symbols:
                    for tf in tfs:
                        inst_id = _instrument_id(v, vcfg, sym, "perp")
                        wm = None if args.force else await catalog.get_watermark(
                            str(vcfg["venue"]), "ohlcv", inst_id, tf
                        )
                        cursor = max(start_ms, (wm + 1) if wm is not None else start_ms)
                        if cursor >= end_ms:
                            print(f"[{v}] {sym} {tf} 已是最新")
                            continue
                        bars = await adapter.fetch_ohlcv(sym, tf, cursor, end_ms)
                        n = store.write_ohlcv(bars)
                        if bars:
                            await catalog.set_watermark(
                                str(vcfg["venue"]), "ohlcv", inst_id, max(b.ts_event_ms for b in bars), tf
                            )
                        print(f"[{v}] {sym} {tf}: 写入 {n} 根 (请求 {len(bars)})")
            finally:
                await adapter.close()
    finally:
        await catalog.close()
    return 0


async def cmd_backfill_trade(args: argparse.Namespace) -> int:
    """回填美股历史成交（Alpaca → canonical/trade）。支持 --start/--end 或 --days。"""
    collect = load_collect(args.config)
    data_dir = _resolve_data_dir(collect, args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = _resolve_catalog(collect, data_dir, args.catalog)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    raw = RawArchive(data_dir)
    store = CanonicalStore(data_dir)
    catalog = Catalog(str(catalog_path))
    await catalog.open()

    start_ms, end_ms = _resolve_tick_window(args)
    print(
        f"[trade] 时间窗 [{_fmt_ms(start_ms)}, {_fmt_ms(end_ms)})（水位之后增量）",
        flush=True,
    )
    sources = _resolve_sources(collect, args, default_sources=["alpaca"])

    try:
        for v in sources:
            if v != "alpaca":
                print(f"[{v}] 跳过 trade 历史回填（仅支持 alpaca）")
                continue
            vcfg = load_source(v)
            adapter = _build_adapter(v, vcfg, raw)
            if not isinstance(adapter, AlpacaAdapter):
                print(f"[{v}] 跳过 trade（适配器无历史成交接口）")
                await adapter.close()
                continue
            symbols = list(vcfg.get("symbols") or [])
            try:
                for sym in symbols:
                    inst_id = _instrument_id(v, vcfg, sym, "stock")
                    wm = None if args.force else await catalog.get_watermark(
                        str(vcfg["venue"]), "trade", inst_id, ""
                    )
                    cursor = max(start_ms, (wm + 1) if wm is not None else start_ms)
                    if cursor >= end_ms:
                        print(f"[{v}] {sym} trade 已是最新（水位/窗口内无新数据）")
                        continue
                    if wm is not None and cursor > start_ms:
                        print(
                            f"[{v}] {sym} trade: 水位 {_fmt_ms(wm)} → 从 {_fmt_ms(cursor)} 继续",
                            flush=True,
                        )
                    t0 = time.perf_counter()
                    ticks = await adapter.fetch_trades_hist(sym, cursor, end_ms)
                    n = store.write_trades(ticks)
                    if ticks:
                        await catalog.set_watermark(
                            str(vcfg["venue"]), "trade", inst_id, max(t.ts_event_ms for t in ticks), ""
                        )
                    elapsed = time.perf_counter() - t0
                    print(
                        f"[{v}] {sym} trade: 写入 {n} 条 (请求 {len(ticks)}), "
                        f"耗时 {elapsed:.1f}s",
                        flush=True,
                    )
            finally:
                await adapter.close()
    finally:
        await catalog.close()
    return 0


async def cmd_backfill_quote(args: argparse.Namespace) -> int:
    """回填美股 L1 报价（Alpaca → canonical/quote）。支持 --start/--end 或 --days。"""
    collect = load_collect(args.config)
    data_dir = _resolve_data_dir(collect, args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = _resolve_catalog(collect, data_dir, args.catalog)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    raw = RawArchive(data_dir)
    store = CanonicalStore(data_dir)
    catalog = Catalog(str(catalog_path))
    await catalog.open()

    start_ms, end_ms = _resolve_tick_window(args)
    print(
        f"[quote] 时间窗 [{_fmt_ms(start_ms)}, {_fmt_ms(end_ms)})（水位之后增量）",
        flush=True,
    )
    sources = _resolve_sources(collect, args, default_sources=["alpaca"])

    try:
        for v in sources:
            if v != "alpaca":
                print(f"[{v}] 跳过 quote 历史回填（仅支持 alpaca）")
                continue
            vcfg = load_source(v)
            adapter = _build_adapter(v, vcfg, raw)
            if not isinstance(adapter, AlpacaAdapter):
                print(f"[{v}] 跳过 quote（适配器无历史报价接口）")
                await adapter.close()
                continue
            symbols = list(vcfg.get("symbols") or [])
            try:
                for sym in symbols:
                    inst_id = _instrument_id(v, vcfg, sym, "stock")
                    wm = None if args.force else await catalog.get_watermark(
                        str(vcfg["venue"]), "quote", inst_id, ""
                    )
                    cursor = max(start_ms, (wm + 1) if wm is not None else start_ms)
                    if cursor >= end_ms:
                        print(f"[{v}] {sym} quote 已是最新（水位/窗口内无新数据）")
                        continue
                    if wm is not None and cursor > start_ms:
                        print(
                            f"[{v}] {sym} quote: 水位 {_fmt_ms(wm)} → 从 {_fmt_ms(cursor)} 继续",
                            flush=True,
                        )
                    t0 = time.perf_counter()
                    quotes = await adapter.fetch_quotes(sym, cursor, end_ms)
                    n = store.write_quotes(quotes)
                    if quotes:
                        await catalog.set_watermark(
                            str(vcfg["venue"]), "quote", inst_id, max(q.ts_event_ms for q in quotes), ""
                        )
                    elapsed = time.perf_counter() - t0
                    print(
                        f"[{v}] {sym} quote: 写入 {n} 条 (请求 {len(quotes)}), "
                        f"耗时 {elapsed:.1f}s",
                        flush=True,
                    )
            finally:
                await adapter.close()
    finally:
        await catalog.close()
    return 0


async def cmd_backfill_fundamental(args: argparse.Namespace) -> int:
    """回填公司基本面（Finnhub → canonical/fundamental）。"""
    collect = load_collect(args.config)
    data_dir = _resolve_data_dir(collect, args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = _resolve_catalog(collect, data_dir, args.catalog)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    raw = RawArchive(data_dir)
    store = CanonicalStore(data_dir)
    catalog = Catalog(str(catalog_path))
    await catalog.open()

    start_ms, end_ms = _resolve_time_window(
        args, default_days=None, allow_open_start=True
    )
    if start_ms is None:
        print(f"[fundamental] 时间窗 (-∞, {_fmt_ms(end_ms)})（水位之后增量）", flush=True)
    else:
        print(
            f"[fundamental] 时间窗 [{_fmt_ms(start_ms)}, {_fmt_ms(end_ms)})（水位之后增量）",
            flush=True,
        )

    sources = _resolve_sources(collect, args, default_sources=["finnhub"])
    try:
        for v in sources:
            if v not in _FUNDAMENTAL_SOURCES:
                print(f"[{v}] 跳过 fundamental（仅支持 finnhub）")
                continue
            vcfg = load_source(v)
            adapter = _build_finnhub(vcfg, raw)
            symbols = list(vcfg.get("symbols") or [])
            try:
                for sym in symbols:
                    inst_id = _instrument_id(v, vcfg, sym, "stock")
                    wm = None if args.force else await catalog.get_watermark(
                        str(vcfg["venue"]), "fundamental", inst_id, ""
                    )
                    cursor = start_ms
                    if wm is not None and cursor is not None:
                        cursor = max(cursor, wm + 1)
                    elif wm is not None and cursor is None:
                        cursor = wm + 1
                    points = await adapter.fetch_fundamentals(
                        sym, start_ms=cursor, end_ms=end_ms
                    )
                    n = store.write_fundamental(points)
                    if points:
                        await catalog.set_watermark(
                            str(vcfg["venue"]),
                            "fundamental",
                            inst_id,
                            max(p.ts_event_ms for p in points),
                            "",
                        )
                    print(f"[{v}] {sym} fundamental: 写入 {n} 条 (请求 {len(points)})")
            finally:
                await adapter.close()
    finally:
        await catalog.close()
    return 0


async def cmd_backfill_macro(args: argparse.Namespace) -> int:
    """回填宏观序列（FRED → canonical/macro）。"""
    collect = load_collect(args.config)
    data_dir = _resolve_data_dir(collect, args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = _resolve_catalog(collect, data_dir, args.catalog)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    raw = RawArchive(data_dir)
    store = CanonicalStore(data_dir)
    catalog = Catalog(str(catalog_path))
    await catalog.open()

    start_ms, end_ms = _resolve_time_window(
        args, default_days=None, allow_open_start=True
    )
    if start_ms is None:
        print(f"[macro] 时间窗 (-∞, {_fmt_ms(end_ms)})（水位之后增量）", flush=True)
    else:
        print(
            f"[macro] 时间窗 [{_fmt_ms(start_ms)}, {_fmt_ms(end_ms)})（水位之后增量）",
            flush=True,
        )

    sources = _resolve_sources(collect, args, default_sources=["fred"])
    only_series = [args.series] if args.series else None
    try:
        for v in sources:
            if v not in _MACRO_SOURCES:
                print(f"[{v}] 跳过 macro（仅支持 fred）")
                continue
            vcfg = load_source(v)
            adapter = _build_fred(vcfg, raw)
            series_ids = only_series or adapter.series_ids()
            try:
                for sid in series_ids:
                    if sid not in adapter.series:
                        print(f"[{v}] 未知 series: {sid}，跳过")
                        continue
                    inst_id = _instrument_id(v, vcfg, sid, "macro")
                    wm = None if args.force else await catalog.get_watermark(
                        str(vcfg["venue"]), "macro", inst_id, ""
                    )
                    cursor = start_ms
                    if wm is not None and cursor is not None:
                        cursor = max(cursor, wm + 1)
                    elif wm is not None and cursor is None:
                        cursor = wm + 1
                    points = await adapter.fetch_macro(
                        sid, start_ms=cursor, end_ms=end_ms
                    )
                    n = store.write_macro(points)
                    if points:
                        await catalog.set_watermark(
                            str(vcfg["venue"]),
                            "macro",
                            inst_id,
                            max(p.ts_event_ms for p in points),
                            "",
                        )
                    print(f"[{v}] {sid} macro: 写入 {n} 条 (请求 {len(points)})")
            finally:
                await adapter.close()
    finally:
        await catalog.close()
    return 0


async def cmd_sync_funding(args: argparse.Namespace) -> int:
    """同步永续资金费率历史；非永续 source（如 alpaca）会跳过。"""
    collect = load_collect(args.config)
    data_dir = _resolve_data_dir(collect, args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = _resolve_catalog(collect, data_dir, args.catalog)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    raw = RawArchive(data_dir)
    store = CanonicalStore(data_dir)
    catalog = Catalog(str(catalog_path))
    await catalog.open()

    start_ms, end_ms = _resolve_time_window(
        args, default_days=int(collect.get("backfill_days", 7))
    )
    assert start_ms is not None
    print(
        f"[funding] 时间窗 [{_fmt_ms(start_ms)}, {_fmt_ms(end_ms)})（水位之后增量）",
        flush=True,
    )
    sources = _resolve_sources(collect, args)

    try:
        for v in sources:
            if v not in _MARKET_SOURCES:
                print(f"[{v}] 跳过 funding（非行情 source）")
                continue
            vcfg = load_source(v)
            if vcfg.get("market_type") != "perp":
                print(f"[{v}] 跳过 funding（非永续合约无资金费率）")
                continue
            adapter = _build_adapter(v, vcfg, raw)
            symbols = list(vcfg.get("symbols") or [])
            try:
                for sym in symbols:
                    inst_id = _instrument_id(v, vcfg, sym, "perp")
                    wm = None if args.force else await catalog.get_watermark(
                        str(vcfg["venue"]), "funding", inst_id, ""
                    )
                    cursor = max(start_ms, (wm + 1) if wm is not None else start_ms)
                    if cursor >= end_ms:
                        print(f"[{v}] {sym} funding 已是最新")
                        continue
                    rows = await adapter.fetch_funding(sym, cursor, end_ms)
                    n = store.write_funding(rows)
                    if rows:
                        await catalog.set_watermark(
                            str(vcfg["venue"]), "funding", inst_id, max(r.ts_event_ms for r in rows), ""
                        )
                    print(f"[{v}] {sym} funding: 写入 {n} 条")
            finally:
                await adapter.close()
    finally:
        await catalog.close()
    return 0


async def cmd_stream_trades(args: argparse.Namespace) -> int:
    """订阅实时成交流并批量写入；股票 source 本阶段跳过。"""
    collect = load_collect(args.config)
    data_dir = _resolve_data_dir(collect, args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    raw = RawArchive(data_dir)
    store = CanonicalStore(data_dir)
    sources = _resolve_sources(collect, args)
    duration = float(args.duration) if args.duration else None

    async def _run_one(v: str) -> None:
        if v not in _MARKET_SOURCES:
            print(f"[{v}] 跳过 trades stream（非行情 source）")
            return
        vcfg = load_source(v)
        if vcfg.get("market_type") == "stock" or v == "alpaca":
            print(f"[{v}] 跳过 trades stream（本阶段未接入）")
            return
        adapter = _build_adapter(v, vcfg, raw)
        symbols = list(vcfg.get("symbols") or [])
        buf: list = []
        flush_every = 50

        async def _consume(sym: str) -> None:
            nonlocal buf
            async for tick in adapter.stream_trades(sym):
                buf.append(tick)
                if len(buf) >= flush_every:
                    store.write_trades(buf)
                    print(f"[{v}] flush {len(buf)} trades ({sym})")
                    buf = []

        try:
            tasks = [asyncio.create_task(_consume(s)) for s in symbols]
            if duration:
                await asyncio.sleep(duration)
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                await asyncio.gather(*tasks)
        finally:
            if buf:
                n = store.write_trades(buf)
                print(f"[{v}] final flush {n} trades")
            await adapter.close()

    await asyncio.gather(*[_run_one(v) for v in sources])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alpha", description="alpha-research-engine 研究与回测 CLI")
    p.add_argument("--config", default=None, help="collect.yaml 路径")
    p.add_argument("--data-dir", default=None, help="数据根目录")
    p.add_argument("--catalog", default=None, help="catalog.sqlite 路径")

    root_sub = p.add_subparsers(dest="command", required=True)
    collect = root_sub.add_parser("collect", help="数据采集与数据湖管理")
    sub = collect.add_subparsers(dest="collect_cmd", required=True)

    # catalog refresh-instruments
    cat = sub.add_parser("catalog", help="catalog 相关")
    cat_sub = cat.add_subparsers(dest="catalog_cmd", required=True)
    ri = cat_sub.add_parser("refresh-instruments", help="刷新标的注册表")
    _add_source_filter_args(ri)

    bf = sub.add_parser("backfill", help="历史回填")
    bf_sub = bf.add_subparsers(dest="backfill_cmd", required=True)
    ohlcv = bf_sub.add_parser("ohlcv", help="回填 K 线")
    _add_source_filter_args(ohlcv)
    ohlcv.add_argument("--tf", default=None, help="如 1h")
    _add_time_window_args(
        ohlcv, days_help="近 N 天（无 --start 时用）；默认读 collect.backfill_days 或 7"
    )

    trade_bf = bf_sub.add_parser("trade", help="回填美股历史成交（Alpaca）")
    _add_source_filter_args(trade_bf)
    _add_time_window_args(trade_bf, days_help="近 N 天（无 --start 时用），默认 1")

    quote_bf = bf_sub.add_parser("quote", help="回填美股 L1 报价（Alpaca）")
    _add_source_filter_args(quote_bf)
    _add_time_window_args(quote_bf, days_help="近 N 天（无 --start 时用），默认 1")

    funda = bf_sub.add_parser("fundamental", help="回填公司基本面（Finnhub）")
    _add_source_filter_args(funda)
    _add_time_window_args(
        funda, days_help="只保留近 N 天报告期；与 --start 二选一；皆无则尽量全量"
    )

    macro = bf_sub.add_parser("macro", help="回填宏观序列（FRED）")
    _add_source_filter_args(macro)
    macro.add_argument("--series", default=None, help="单个 FRED series_id，默认配置全部")
    _add_time_window_args(
        macro, days_help="只保留近 N 天观测；与 --start 二选一；皆无则尽量全量"
    )

    sync = sub.add_parser("sync", help="增量同步")
    sync_sub = sync.add_subparsers(dest="sync_cmd", required=True)
    fund = sync_sub.add_parser("funding", help="同步资金费率")
    _add_source_filter_args(fund)
    _add_time_window_args(
        fund, days_help="近 N 天（无 --start 时用）；默认读 collect.backfill_days 或 7"
    )

    st = sub.add_parser("stream", help="实时流")
    st_sub = st.add_subparsers(dest="stream_cmd", required=True)
    tr = st_sub.add_parser("trades", help="订阅成交")
    _add_source_filter_args(tr)
    tr.add_argument("--duration", type=float, default=None, help="运行秒数，默认一直跑")

    feature = root_sub.add_parser("feature", help="特征计算")
    feature_sub = feature.add_subparsers(dest="feature_cmd", required=True)
    feature_run = feature_sub.add_parser("run", help="计算并落盘特征集")
    feature_run.add_argument("--research-config", required=True, help="研究 YAML 路径")

    backtest = root_sub.add_parser("backtest", help="bar 级回测")
    backtest_sub = backtest.add_subparsers(dest="backtest_cmd", required=True)
    backtest_run = backtest_sub.add_parser("run", help="运行研究 YAML 声明的策略与回测")
    backtest_run.add_argument("--research-config", required=True, help="研究 YAML 路径")

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "feature" and args.feature_cmd == "run":
        code = cmd_feature_run(args)
    elif args.command == "backtest" and args.backtest_cmd == "run":
        code = cmd_backtest_run(args)
    elif args.command == "collect" and args.collect_cmd == "catalog" and args.catalog_cmd == "refresh-instruments":
        code = asyncio.run(cmd_refresh_instruments(args))
    elif args.command == "collect" and args.collect_cmd == "backfill" and args.backfill_cmd == "ohlcv":
        code = asyncio.run(cmd_backfill_ohlcv(args))
    elif args.command == "collect" and args.collect_cmd == "backfill" and args.backfill_cmd == "trade":
        code = asyncio.run(cmd_backfill_trade(args))
    elif args.command == "collect" and args.collect_cmd == "backfill" and args.backfill_cmd == "quote":
        code = asyncio.run(cmd_backfill_quote(args))
    elif args.command == "collect" and args.collect_cmd == "backfill" and args.backfill_cmd == "fundamental":
        code = asyncio.run(cmd_backfill_fundamental(args))
    elif args.command == "collect" and args.collect_cmd == "backfill" and args.backfill_cmd == "macro":
        code = asyncio.run(cmd_backfill_macro(args))
    elif args.command == "collect" and args.collect_cmd == "sync" and args.sync_cmd == "funding":
        code = asyncio.run(cmd_sync_funding(args))
    elif args.command == "collect" and args.collect_cmd == "stream" and args.stream_cmd == "trades":
        code = asyncio.run(cmd_stream_trades(args))
    else:
        parser.print_help()
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
