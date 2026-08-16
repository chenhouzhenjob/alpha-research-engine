"""对"一批候选池"算复合分：收集原始指标 → 在候选集内做 min-max 归一化 → 逐池计算
`CompositeScoreResult`。从 `lp_backtest.calibrate.weights` 抽出来的共用逻辑——`live-signal`
的 `/conclusion` 端点需要同一套编排（复合分本质是候选集内的相对排名，不能只算一个池子，
见 `models/normalize.py` 的文档），这是继 `assemble_daily_metrics` 之后第二处"同一套编排逻辑
被多个调用方需要"的真实重复，不是提前抽象。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from alpha_chains.base import ChainAdapter
from alpha_core.metrics import MetricValue
from alpha_core.types import Chain
from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin
from alpha_storage.db import session_scope
from alpha_storage.models import PoolCandidateRow
from alpha_storage.repositories.pool_metrics import PoolMetricsRepository

from .assemble import assemble_daily_metrics
from .models.composite_score import (
    NORMALIZED_COMPONENT_WEIGHTS,
    CompositeScoreResult,
    compute_composite_score,
)
from .models.normalize import normalize_min_max

# NORMALIZED_COMPONENT_WEIGHTS 的键 -> PoolRawMetrics 对应字段名（"il_risk" 存的是
# |ExpectedIL_ref|，字段名叫 il_risk_raw 以强调"还没归一化"，其余三个字段名和分项键正好一致）。
_FIELD_BY_COMPONENT: dict[str, str] = {
    "nominal_apr": "nominal_apr",
    "vt_ratio": "vt_ratio",
    "il_risk": "il_risk_raw",
    "capital_volatility": "capital_volatility",
}


@dataclass(frozen=True)
class PoolRawMetrics:
    """一个候选池当前的原始指标值（未归一化）。"""

    pool_address: str
    nominal_apr: float | None
    vt_ratio: float | None
    il_risk_raw: float | None  # |ExpectedIL_ref|，见 composite_score.py 的符号澄清
    capital_volatility: float | None
    age_penalty: float | None


def collect_raw_metrics(
    pools: list[PoolCandidateRow],
    *,
    chain: Chain,
    adapter: ChainAdapter,
    plugin: PancakeswapV3Plugin,
    cake_usd_price: float | None,
    as_of: date | None = None,
) -> list[PoolRawMetrics]:
    """@param as_of 默认今天；`live-signal` 传入请求的 `as_of` 参数，`calibrate/weights` 用默认值"""
    as_of = as_of if as_of is not None else date.today()
    results = []
    with session_scope() as session:
        metrics_repo = PoolMetricsRepository(session)
        for row in pools:
            history = metrics_repo.get_series_up_to(chain, row.pool_address, as_of)
            m = assemble_daily_metrics(
                chain=chain,
                pool_address=row.pool_address,
                fee_pips=row.fee_pips,
                created_at=row.created_at,
                as_of=as_of,
                history=history,
                adapter=adapter,
                plugin=plugin,
                cake_usd_price=cake_usd_price,
            )
            results.append(
                PoolRawMetrics(
                    pool_address=row.pool_address,
                    nominal_apr=m.nominal_apr.value if m.nominal_apr.is_available else None,
                    vt_ratio=m.vt_ratio.value if m.vt_ratio.is_available else None,
                    il_risk_raw=abs(m.expected_il_ref.value) if m.expected_il_ref.is_available else None,
                    capital_volatility=(
                        m.capital_volatility.value if m.capital_volatility.is_available else None
                    ),
                    age_penalty=m.age_penalty.value if m.age_penalty.is_available else None,
                )
            )
    return results


def normalize_component(raw_metrics: list[PoolRawMetrics], component: str) -> dict[str, float]:
    """在"该分项当前可得"的候选池子集内做 min-max 归一化，返回 {地址: 归一化值}。"""
    field = _FIELD_BY_COMPONENT[component]
    pairs = [(m.pool_address, getattr(m, field)) for m in raw_metrics if getattr(m, field) is not None]
    if not pairs:
        return {}
    addrs, values = zip(*pairs, strict=True)
    normalized = normalize_min_max(list(values))
    return dict(zip(addrs, normalized, strict=True))


def normalize_all_components(raw_metrics: list[PoolRawMetrics]) -> dict[str, dict[str, float]]:
    """对全部 4 个可归一化分项各自做一次候选集内归一化。@returns {分项名: {地址: 归一化值}}——
    单独暴露这一步（而不是把它内嵌进 `compute_composite_scores`）是因为 `calibrate/weights`
    的报告还需要每个分项各自的覆盖率/相关性，不只是最终分数。
    """
    return {component: normalize_component(raw_metrics, component) for component in NORMALIZED_COMPONENT_WEIGHTS}


def compute_composite_scores(
    raw_metrics: list[PoolRawMetrics], normalized_by_field: dict[str, dict[str, float]]
) -> dict[str, CompositeScoreResult]:
    """对整个候选集逐池计算复合分。@param normalized_by_field 来自 `normalize_all_components`
    @returns {pool_address: CompositeScoreResult}
    """
    scores: dict[str, CompositeScoreResult] = {}
    for m in raw_metrics:
        normalized_components = {
            component: normalized_by_field[component].get(m.pool_address)
            for component in NORMALIZED_COMPONENT_WEIGHTS
        }
        scores[m.pool_address] = compute_composite_score(normalized_components, m.age_penalty)
    return scores


def compute_percentile_ranks(scores: dict[str, CompositeScoreResult]) -> dict[str, MetricValue[float]]:
    """退出信号 4"相对排名坍塌"用（`pool-discovery-metrics-v1.md` §3.2：
    "该池子当前 CompositeScore 在全量候选池中的百分位 < 阈值（如后 50%）"）。

    @returns {pool_address: 百分位}，0.0 = 候选集里分最低，1.0 = 分最高（含并列时按排序位置，
    不做插值）。候选池数 < 2 时百分位没有意义（跟 `models/README.md` 里
    `normalize_min_max` 的同一条道理："候选集只有 2 个时归一化会失真"，这里是候选集
    只有 1 个时排名根本无从谈起），全部标记 unavailable。
    """
    if len(scores) < 2:
        return {
            addr: MetricValue.unavailable(f"候选池数量 {len(scores)} < 2，百分位排名无意义")
            for addr in scores
        }
    ranked = sorted(scores.items(), key=lambda kv: kv[1].score)
    denominator = len(ranked) - 1
    return {addr: MetricValue.available(idx / denominator) for idx, (addr, _result) in enumerate(ranked)}
