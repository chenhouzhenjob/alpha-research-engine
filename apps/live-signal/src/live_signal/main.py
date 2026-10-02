"""FastAPI 应用：`GET /conclusion?chain=bsc&pool=0x...&as_of=latest`。

阶段 1（`research/docs/live-signal-system-设计方案.md` 第 6/11 章）：`exit_signals`/
`recommended_range` 已经是真实计算，不再是阶段 0 的占位——`exit_signals` 见
`alpha_metrics.models.exit_signals`，`recommended_range` 见下面 `_compute_recommended_range`。
`recommended_action` 规则：5 条退出信号任意一条"可用且触发" → `exit`；否则退回
`CompositeScoreResult.tier` 的简单映射（S/A → hold，B/C → no_signal）——**不在这里判断
`rebalance`**：research 不知道调用方当前仓位的实际 tick 范围，"现在的区间跟推荐的差多少、
值不值得为了 gas 成本去调"这个比较只有调用方自己能做，`recommended_range` 给的就是这个
判断所需的数据。
`risk_flags` 仍然恒为 `{}`——RWA 参考价数据源、TrackingError 等特征是阶段 2 的事，这一项
在响应里保留占位。

`research` 侧只读、无副作用：这个端点只查询已有数据 + 现场读一次链上只读状态（feeProtocol/
CAKE 排放/decimals 之类，全部是 `eth_call`），不写任何数据、不触发任何交易。

**候选集范围（真实跑过才发现的问题，不是理论谨慎）**：CompositeScore 是候选集内的相对排名
（见 `alpha_metrics.models.normalize`），`calibrate/weights` 原本的语义是"DB 里全部
`status=qualified` 的池子"——但 `pool_candidates` 混杂了两种不同目的的池子：1a-1f 阶段自动
发现/校准用的历史候选池（现在有 28 个），和这次阶段 0 手动注册的实时试跑池子（`register_pool.py`
注册的 2 个）。直接复用"全部 qualified"当候选集，实测一次请求要对 28 个池子各查好几次链上状态
（feeProtocol + CAKE 排放），单次请求超过 180 秒——不仅慢到不可用（这个端点设计上是要被
alpha-lp 高频轮询的），拿一堆无关的历史测试池子来对比也没有意义。
用 `LIVE_SIGNAL_POOL_ADDRESSES` 环境变量（逗号分隔）显式限定候选集，不设默认回退到全部
qualified——阶段 0 部署时应该配置成这次注册的试跑池子地址。这跟 `packages/chains` 的多端点配置、
`packages/datasources` 的数据源选择是同一个"可插拔/配置驱动"的思路，不是新发明。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

from alpha_chains.bsc import build_bsc_adapter
from alpha_core.instrument_id import MarketType, build_instrument_id
from alpha_core.metrics import MetricValue
from alpha_core.types import AssetClass, Chain, MetricAvailability, PoolCandidateStatus
from alpha_datasources.coingecko import COINGECKO_CAKE_ID, CoinGeckoClient
from alpha_metrics.assemble import assemble_daily_metrics
from alpha_metrics.chain_reads import read_decimals_cached, read_slot0_price_and_tick_safe
from alpha_metrics.compute import PoolDailyMetrics
from alpha_metrics.features._ohlcv_window import fetch_recent_1m_candles, resample_to_5m
from alpha_metrics.features.adx import adx
from alpha_metrics.features.atr import atr_pct_series
from alpha_metrics.models.exit_signals import ExitSignals, any_signal_fired, compute_exit_signals
from alpha_metrics.models.recommended_range import MAINTENANCE_PROFILES, capital_efficiency, recommended_width
from alpha_metrics.models.trend_state import ATR_PCT_HISTORY_WINDOW, select_maintenance_profile
from alpha_metrics.scoring import (
    collect_raw_metrics,
    compute_composite_scores,
    compute_percentile_ranks,
    normalize_all_components,
)
from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin
from alpha_protocols.tick_math import price_to_tick, round_to_tick_spacing
from alpha_storage.db import session_scope
from alpha_storage.models import PoolCandidateRow
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository
from alpha_storage.repositories.pool_metrics import PoolMetricsRepository
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

from .background import ingest as ingest_bg
from .background import poll_ohlcv as poll_ohlcv_bg
from .background import subscribe as subscribe_bg

logger = logging.getLogger(__name__)
load_dotenv()

MODEL_VERSION = "live-signal-v0.1.0-phase1"
POOL_ADDRESSES_ENV = "LIVE_SIGNAL_POOL_ADDRESSES"
# WSS 逐笔订阅默认关闭——K 线已经改用 poll_ohlcv 轮询 GeckoTerminal，不再依赖 swap_events，
# 逐笔数据现在只服务"以后的风控信号"这一个用途，暂时不需要就先不跑，省 WS 流量成本
# （NodeReal 按字节计费，见部署记录）。要启用时把这个环境变量设成 "1"/"true" 即可。
ENABLE_WSS_ENV = "LIVE_SIGNAL_ENABLE_WSS_SUBSCRIBE"

# S/A → hold，B/C → no_signal——5 条退出信号都没触发时的兜底映射，不是真决策模型的全部
# （见模块文档："exit" 优先于这个映射）。
_TIER_TO_ACTION = {"S": "hold", "A": "hold", "B": "no_signal", "C": "no_signal"}

# 跟 `background/poll_ohlcv.py` 用同一个字面量构造 instrument_id——阶段 0 只有一个协议，
# 不从 `pool_candidates.dex_id` 读回来比较（CHAR(30) 尾随空格 padding 的坑，见该模块注释）。
_PANCAKESWAP_V3_BSC_VENUE = "pancakeswap-v3-bsc"

# `recommended_range.profile` 对外用英文——这是给调用方消费的外部契约，不该要求它认识中文枚举值；
# 内部 `MAINTENANCE_PROFILES` 的中文 key 不改（`1d` 回测代码已经在用，改了影响面更大）。
_PROFILE_KEY_TO_ENGLISH = {"主动型": "active", "平衡型": "balanced", "被动型": "passive"}


def _load_pool_addresses() -> list[str]:
    """解析 `LIVE_SIGNAL_POOL_ADDRESSES`（逗号分隔），供 `/conclusion` 端点和后台任务共用，
    不在两处各写一份 split/strip/lower 逻辑。
    """
    raw = os.environ.get(POOL_ADDRESSES_ENV, "")
    return [addr.strip().lower() for addr in raw.split(",") if addr.strip()]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动/停止常驻后台任务（WSS 订阅、K 线轮询、每日历史快照回填）——都跟 HTTP
    请求处理共用同一个进程/事件循环，不是三个独立的 cron 触发的短命 CLI，见
    `background/__init__.py` 的说明。阶段 0 不做进程级"保活"，这个进程本身崩溃/重启
    由外部工具负责，不在这里处理。
    """
    pool_addresses = _load_pool_addresses()
    tasks: list[asyncio.Task] = []
    if pool_addresses:
        tasks.append(asyncio.create_task(poll_ohlcv_bg.run_forever(pool_addresses)))
        tasks.append(asyncio.create_task(ingest_bg.run_forever(pool_addresses)))
        if os.environ.get(ENABLE_WSS_ENV, "").strip().lower() in ("1", "true", "yes"):
            tasks.append(asyncio.create_task(subscribe_bg.run_forever(pool_addresses)))
        else:
            logger.info("WSS 逐笔订阅未启用（%s 未设为真值），跳过", ENABLE_WSS_ENV)
    else:
        logger.warning("%s 未配置，后台任务（K 线轮询/历史快照回填/WSS 订阅）全部不启动", POOL_ADDRESSES_ENV)

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="live-signal", version=MODEL_VERSION, lifespan=lifespan)


def _collect_rationale(metrics: PoolDailyMetrics) -> list[str]:
    """从 `PoolDailyMetrics` 里每个 `UNAVAILABLE`（真的算不出来）指标的 `reason` 拼出
    rationale——用的是真实计算过程里记录下来的原因（`MetricValue.unavailable(reason)`），
    不是编造的话术。

    只收 `UNAVAILABLE`，不收 `NO_INCENTIVE`：`NO_INCENTIVE`（如"该池没有 CAKE farm"）是一个
    确认过的正常状态，大多数池子本来就没有 CAKE 激励，不是"数据缺失"，混进 rationale 会显得
    像是在警示某种异常，实际只是这个池子的正常特征，见 alpha_core.metrics 的三态约定。
    """
    fields: list[MetricValue] = [
        metrics.fee_apr,
        metrics.cake_apr,
        metrics.vt_ratio,
        metrics.sigma_price,
        metrics.expected_il_ref,
        metrics.capital_volatility,
        metrics.age_penalty,
        metrics.depth_tier,
    ]
    return [f.reason for f in fields if f.availability == MetricAvailability.UNAVAILABLE and f.reason]


def _value_or_none(metric: MetricValue) -> object:
    """`MetricValue` 序列化成 JSON 的统一方式：可用给真实值，不可用/无激励一律给 `null`——
    调用方不该去读 `reason` 字符串做逻辑判断，那是给人看的排查信息，见 `rationale` 字段。
    """
    return metric.value if metric.is_available else None


def _compute_recommended_range(
    *,
    daily_metrics: PoolDailyMetrics,
    adx_value: MetricValue[float],
    atr_pct_now: MetricValue[float],
    atr_pct_history: list[float],
    current_price_and_tick: tuple[float, int] | None,
    candidate: PoolCandidateRow,
    decimals0: int,
    decimals1: int,
) -> dict | None:
    """选维护档位（`models.trend_state`）→ 反推区间宽度（`models.recommended_range`，
    复用已有的区间数学，不重新发明）→ 换算成 tick（`alpha_protocols.tick_math`，阶段 1 新写）。

    ADX/ATR%/σ_price/当前价格 任一不可用 → 整体返回 `None`（不能在信息不全的情况下悄悄给出
    一个区间建议）。**不管 `recommended_action` 是什么都算这个字段**——调用方需要这个数据点去
    跟自己当前仓位比较，不该被 research 这边的 action 判断卡住。
    """
    maintenance_profile = select_maintenance_profile(adx_value, atr_pct_now, atr_pct_history)
    if not maintenance_profile.is_available:
        return None
    if not daily_metrics.sigma_price.is_available:
        return None
    if current_price_and_tick is None:
        return None

    current_price, current_tick = current_price_and_tick
    t_target_days = MAINTENANCE_PROFILES[maintenance_profile.value]
    width = recommended_width(daily_metrics.sigma_price.value, t_target_days)
    price_upper = current_price * math.exp(width)
    price_lower = current_price * math.exp(-width)
    tick_upper = round_to_tick_spacing(price_to_tick(price_upper, decimals0, decimals1), candidate.tick_spacing)
    tick_lower = round_to_tick_spacing(price_to_tick(price_lower, decimals0, decimals1), candidate.tick_spacing)

    return {
        "profile": _PROFILE_KEY_TO_ENGLISH[maintenance_profile.value],
        "target_days": t_target_days,
        "current_price": current_price,
        "current_tick": current_tick,
        "price_lower": price_lower,
        "price_upper": price_upper,
        "tick_lower": tick_lower,
        "tick_upper": tick_upper,
        "capital_efficiency": capital_efficiency(width),
    }


def _exit_signals_json(signals: ExitSignals) -> dict:
    return {
        "net_edge_negative": _value_or_none(signals.net_edge_negative),
        "net_edge_value": _value_or_none(signals.net_edge_value),
        "cake_incentive_withdrawn": _value_or_none(signals.cake_incentive_withdrawn),
        "vtratio_ma7_drop_pct": _value_or_none(signals.vtratio_ma7_drop_pct),
        "composite_rank_percentile": _value_or_none(signals.composite_rank_percentile),
        "cumulative_realized_il_exceeds_fees": _value_or_none(signals.cumulative_realized_il_exceeds_fees),
        "realized_il_usd": _value_or_none(signals.realized_il_usd),
    }


@app.get("/conclusion")
def get_conclusion(
    chain: str = Query(..., description="阶段 0 只支持 'bsc'"),
    pool: str = Query(..., description="池子合约地址"),
    as_of: str = Query("latest", description="YYYY-MM-DD，或 'latest'（默认，今天）"),
    position_open_vtratio_ma7: float | None = Query(
        None, description="开仓时 VTRatio 7日均值——退出信号 3（交易量枯竭）的基线，不传则该信号 unavailable"
    ),
    position_open_price: float | None = Query(
        None, description="开仓时 token1/token0 汇率——退出信号 5（累计倒亏）的基线，不传则该信号 unavailable"
    ),
    position_open_value_usd: float | None = Query(
        None, description="开仓时头寸美元价值——退出信号 5 的基线，不传则该信号 unavailable"
    ),
    position_cumulative_fees_usd: float | None = Query(
        None, description="累计已实现手续费+CAKE收入（美元）——退出信号 5 的比较基准，不传则该信号 unavailable"
    ),
) -> dict:
    if chain != Chain.BSC.value:
        raise HTTPException(status_code=400, detail=f"阶段 0 只支持 chain=bsc，收到 {chain!r}")
    chain_enum = Chain.BSC
    pool_address = pool.lower()
    as_of_date = date.today() if as_of == "latest" else date.fromisoformat(as_of)

    allowed_addresses = set(_load_pool_addresses())
    if not allowed_addresses:
        raise HTTPException(
            status_code=500,
            detail=f"环境变量 {POOL_ADDRESSES_ENV} 未配置——阶段 0 需要显式指定候选集，见模块文档",
        )
    if pool_address not in allowed_addresses:
        raise HTTPException(status_code=404, detail=f"{pool_address} 不在 {POOL_ADDRESSES_ENV} 配置的候选集里")

    adapter = build_bsc_adapter()
    plugin = PancakeswapV3Plugin()
    cake_usd_price = CoinGeckoClient().get_simple_price_usd(COINGECKO_CAKE_ID)

    with session_scope() as session:
        candidate_repo = PoolCandidateRepository(session)
        candidate = candidate_repo.get_by_address(chain_enum, pool_address)
        # 候选集用 list_by_status（SQL 层按 status 过滤）取得，不对读回来的 candidate.status 做
        # 原始 Python 字符串比较——CHAR(10) 存 "qualified"（9 个字符）读回来会带一个尾随空格，
        # SQLAlchemy/psycopg 不会自动 strip，直接 `==` 比较会静默判假（instrument_id 从
        # CHAR 改成 TEXT 就是因为踩过这个坑，见 research/SCHEMA.md 第 6 节）。
        qualified_pools = candidate_repo.list_by_status(chain_enum, PoolCandidateStatus.QUALIFIED)
        qualified_addresses = {row.pool_address for row in qualified_pools}
        pools = [row for row in qualified_pools if row.pool_address in allowed_addresses]
        if candidate is None or pool_address not in qualified_addresses:
            raise HTTPException(status_code=404, detail=f"{pool_address} 不是已 qualified 的候选池")
        history = PoolMetricsRepository(session).get_series_up_to(chain_enum, pool_address, as_of_date)
        decimals0 = read_decimals_cached(session, adapter, chain_enum, candidate.token0_address)
        decimals1 = read_decimals_cached(session, adapter, chain_enum, candidate.token1_address)
        instrument_id = build_instrument_id(
            venue=_PANCAKESWAP_V3_BSC_VENUE, market_type=MarketType.DEX_POOL, symbol_raw=pool_address
        )
        candles_1m = fetch_recent_1m_candles(session, instrument_id)

    # 复合分本质是候选集内的相对排名（见 alpha_metrics.models.normalize 的文档），不能只对
    # 这一个池子单独算，要对同一批候选池重新收集一遍全量指标再归一化。
    raw_metrics = collect_raw_metrics(
        pools,
        chain=chain_enum,
        adapter=adapter,
        plugin=plugin,
        cake_usd_price=cake_usd_price,
        as_of=as_of_date,
    )
    normalized_by_field = normalize_all_components(raw_metrics)
    scores = compute_composite_scores(raw_metrics, normalized_by_field)
    score_result = scores.get(pool_address)
    if score_result is None:
        # collect_raw_metrics 对同一批 pools 逐个算，理论上不会漏；真出现说明内部状态有问题。
        raise HTTPException(status_code=500, detail=f"{pool_address} 复合分计算失败（未预期的内部状态）")

    daily_metrics = assemble_daily_metrics(
        chain=chain_enum,
        pool_address=pool_address,
        fee_pips=candidate.fee_pips,
        created_at=candidate.created_at,
        as_of=as_of_date,
        history=history,
        adapter=adapter,
        plugin=plugin,
        cake_usd_price=cake_usd_price,
    )

    # ATR/ADX 用 5 分钟K线（重采样自已经落库的 1 分钟K线，不新增数据源），供趋势/波动率状态
    # 模型选维护档位，也是退出信号之外唯一还需要的"当前市场状态"输入。
    candles_5m = resample_to_5m(candles_1m)
    adx_value = adx(candles_5m)
    atr_pct_full_series = atr_pct_series(candles_5m)
    if atr_pct_full_series:
        atr_pct_now = MetricValue.available(atr_pct_full_series[-1])
        atr_pct_history = atr_pct_full_series[:-1][-ATR_PCT_HISTORY_WINDOW:]
    else:
        atr_pct_now = MetricValue.unavailable(f"5分钟K线不足以算出 ATR%（现有 {len(candles_5m)} 根）")
        atr_pct_history = []

    # 现场查一次链上 slot0（合并 sqrtPriceX96/tick 一次 eth_call），短缓存（15 秒）——价格变化
    # 比 feeProtocol/CAKE 排放快得多，不能像那两项一样缓存 5 分钟（确认过的实现决策 #2）。
    current_price_and_tick = read_slot0_price_and_tick_safe(adapter, plugin, pool_address, decimals0, decimals1)
    current_price = current_price_and_tick[0] if current_price_and_tick is not None else None

    percentile_ranks = compute_percentile_ranks(scores)
    composite_rank_percentile = percentile_ranks.get(
        pool_address, MetricValue.unavailable(f"候选池数量 {len(scores)} < 2，百分位排名无意义")
    )

    exit_signals = compute_exit_signals(
        daily_metrics=daily_metrics,
        history=history,
        current_price=current_price,
        composite_rank_percentile=composite_rank_percentile,
        position_open_vtratio_ma7=position_open_vtratio_ma7,
        position_open_price=position_open_price,
        position_open_value_usd=position_open_value_usd,
        position_cumulative_fees_usd=position_cumulative_fees_usd,
    )

    recommended_range = _compute_recommended_range(
        daily_metrics=daily_metrics,
        adx_value=adx_value,
        atr_pct_now=atr_pct_now,
        atr_pct_history=atr_pct_history,
        current_price_and_tick=current_price_and_tick,
        candidate=candidate,
        decimals0=decimals0,
        decimals1=decimals1,
    )

    action = "exit" if any_signal_fired(exit_signals) else _TIER_TO_ACTION[score_result.tier]

    return {
        "model_version": MODEL_VERSION,
        "as_of": datetime.now(UTC).isoformat(),
        "chain": chain_enum.value,
        "pool_address": pool_address,
        "asset_class": AssetClass(candidate.asset_class.strip()).value,
        "recommended_action": action,
        "recommended_range": recommended_range,
        "exit_signals": _exit_signals_json(exit_signals),
        "risk_flags": {},  # 恒为空——参考价数据源/TrackingError 等 RWA 专属特征是阶段 2 的事
        "composite_score": score_result.score,
        "confidence": score_result.confidence,
        "rationale": _collect_rationale(daily_metrics),
    }


def run() -> None:
    """`live-signal-serve` CLI 入口。"""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8100)


if __name__ == "__main__":
    run()
