"""1e 权重与分档校准 CLI：拿全部 qualified 候选池的当前指标快照，跑一遍
`features/composite_score.py` 的默认权重，输出分项相关性、分数分布、S/A/B/C 分档数量，
以及一份基于真实数据的校准建议。

**只产出建议，不直接改权重**——改默认权重是需要产品/业务方拍板的决策，不是这次分析该自己定的。
"""

from __future__ import annotations

import logging
import math
import statistics

import click
from alpha_chains.bsc import build_bsc_adapter
from alpha_core.types import Chain, PoolCandidateStatus
from alpha_datasources.coingecko import COINGECKO_CAKE_ID, CoinGeckoClient
from alpha_datasources.geckoterminal import GeckoTerminalClient
from alpha_metrics.models.composite_score import NORMALIZED_COMPONENT_WEIGHTS, CompositeScoreResult
from alpha_metrics.scoring import (
    PoolRawMetrics,
    collect_raw_metrics,
    compute_composite_scores,
    normalize_all_components,
)
from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin
from alpha_storage.db import session_scope
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository
from dotenv import load_dotenv

from ..report_utils import pool_label

logger = logging.getLogger(__name__)

_COMPONENT_LABELS = {
    "nominal_apr": "NominalAPR",
    "vt_ratio": "VTRatio",
    "il_risk": "IL 风险幅度",
    "capital_volatility": "CapitalVolatility",
}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """皮尔逊相关系数；样本数 < 2 或某一侧方差为 0 时返回 None（算不出来，不能当 0 处理）。"""
    if len(xs) < 2:
        return None
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _render_report(
    raw_metrics: list[PoolRawMetrics],
    normalized_by_field: dict[str, dict[str, float]],
    scores: dict[str, CompositeScoreResult],
    pool_names: dict[str, str],
    network: str,
) -> str:
    lines = [
        "# 1e：复合分权重与分档校准报告",
        "",
        f"候选池数: {len(raw_metrics)}（全部 qualified 候选池，当前时点单次快照，不是历史面板）。",
        "",
        "## 分项覆盖率（该分项当前有多少池子算得出来）",
        "",
        "| 分项 | 可得池子数 | 覆盖率 |",
        "|---|---|---|",
    ]
    total = len(raw_metrics)
    for field in NORMALIZED_COMPONENT_WEIGHTS:
        n = len(normalized_by_field.get(field, {}))
        lines.append(f"| {_COMPONENT_LABELS[field]} | {n} | {n / total * 100:.0f}% |")
    n_age = sum(1 for m in raw_metrics if m.age_penalty is not None)
    lines.append(f"| AgePenalty | {n_age} | {n_age / total * 100:.0f}% |")
    lines.append("")

    lines += ["## 分项相关性（皮尔逊系数，只在两项都可得的池子上算）", ""]
    fields = list(NORMALIZED_COMPONENT_WEIGHTS)
    lines.append("| | " + " | ".join(_COMPONENT_LABELS[f] for f in fields) + " |")
    lines.append("|---|" + "---|" * len(fields))
    high_correlations = []
    for f1 in fields:
        row = [_COMPONENT_LABELS[f1]]
        for f2 in fields:
            if f1 == f2:
                row.append("1.00")
                continue
            common = set(normalized_by_field.get(f1, {})) & set(normalized_by_field.get(f2, {}))
            if len(common) < 2:
                row.append("n/a")
                continue
            xs = [normalized_by_field[f1][a] for a in common]
            ys = [normalized_by_field[f2][a] for a in common]
            r = _pearson(xs, ys)
            row.append(f"{r:.2f}" if r is not None else "n/a")
            if r is not None and abs(r) >= 0.7 and f1 < f2:
                high_correlations.append((f1, f2, r))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    tier_counts: dict[str, int] = {"S": 0, "A": 0, "B": 0, "C": 0}
    for result in scores.values():
        tier_counts[result.tier] += 1
    score_values = [r.score for r in scores.values()]

    lines += [
        "## 分数分布 / S-A-B-C 分档",
        "",
        f"分数范围: {min(score_values):.4f} ~ {max(score_values):.4f}（均值 {statistics.mean(score_values):.4f}）。",
        "",
        "| 分档 | 数量 | 占比 |",
        "|---|---|---|",
    ]
    for tier in ("S", "A", "B", "C"):
        count = tier_counts[tier]
        lines.append(f"| {tier} | {count} | {count / total * 100:.0f}% |")
    lines.append("")

    lines += ["## 校准建议（基于以上真实数据的发现，不是凭空调整）", ""]

    # 发现 1：原公式的理论可达分数范围和 S/A/B/C 阈值对不上。
    add_weight = sum(w for w, sign in NORMALIZED_COMPONENT_WEIGHTS.values() if sign > 0)
    sub_weight = sum(w for w, sign in NORMALIZED_COMPONENT_WEIGHTS.values() if sign < 0)
    theoretical_max = add_weight  # 扣分项都恰好为 0 时的上限（未考虑 AgePenalty，它只会让分数更低）
    theoretical_min = -sub_weight - 0.10  # 加分项都为 0、扣分项都拉满、AgePenalty 也拉满
    lines.append(
        f"1. **理论可达分数范围和 S/A/B/C 阈值不匹配**：原公式加分项权重合计 {add_weight:.2f}，"
        f"扣分项权重合计 {sub_weight:.2f} + AgePenalty {0.10}，"
        f"理论上限约 {theoretical_max:.2f}（扣分项都恰好是 0 才能达到），理论下限约 {theoretical_min:.2f}。"
        f"S 档阈值 0.75 在这个范围里几乎不可能达到（除非某池子加分项归一化值拉满、扣分项全部为 0）。"
        "这次真实数据跑出来的分数范围是 "
        f"{min(score_values):.4f} ~ {max(score_values):.4f}，"
        + ("印证了这一点——" if max(score_values) < 0.75 else "")
        + "建议二选一：(a) 把 S/A/B/C 阈值按实际可达范围重新标定（更容易落地，但阈值失去「绝对含义」）；"
        "(b) 改公式结构，让加分项权重合计归一到 1（扣分项作为在这个基础上的折扣而不是独立减项），"
        "这样分数天然落在 [0,1] 附近，阈值不用改——(b) 是更彻底的方案但改动面更大，需要产品侧确认。"
    )

    # 发现 2：数据覆盖率问题。
    low_coverage = [
        field for field in NORMALIZED_COMPONENT_WEIGHTS if len(normalized_by_field.get(field, {})) / total < 0.5
    ]
    if low_coverage:
        names = "、".join(_COMPONENT_LABELS[f] for f in low_coverage)
        lines.append(
            f"2. **{names} 当前覆盖率不足一半**，对应权重被系统性重新分摊给其余分项——"
            "这不是这些分项本身没有校准价值，而是 1a 已知的数据积累限制"
            "（CapitalVolatility 需要 14 天真实 TVL 历史，目前只有 1 天）。"
            "等数据积累够之后应该重跑这个校准，现在的相关性分析和分数分布"
            "都是在「这几项系统性缺失」的前提下算出来的，不是终局结论。"
        )

    # 发现 3：AgePenalty 名义权重和实际影响力不成比例。
    lines.append(
        "3. **AgePenalty 的实际影响力远小于名义权重**：它不参与归一化，原始值只有 0 或 0.05，"
        f"乘权重 0.10 之后最多影响分数 {0.05 * 0.10:.4f}，而其余四项每一项都能在 "
        f"[0, 权重] 的范围里连续变化——0.10 的名义权重和不到 0.005 的实际最大影响力不成比例。"
        "建议要么把 AgePenalty 也纳入某种归一化（比如按「距准入门槛 7 天还有多久」连续化，而不是 0/1 两档），"
        "要么干脆承认它只是一个象征性的小惩罚，不需要按 10% 的权重量级去理解。"
    )

    if high_correlations:
        pairs_desc = "；".join(
            f"{_COMPONENT_LABELS[f1]} 和 {_COMPONENT_LABELS[f2]}（r={r:.2f}）" for f1, f2, r in high_correlations
        )
        lines.append(
            f"4. **发现高相关分项**：{pairs_desc}，|r|≥0.7。这两项可能在重复计分同一个信号，"
            "建议 1e 后续迭代时考虑合并或降低其中一项的权重，具体怎么调需要更多样本验证这不是巧合。"
        )
    else:
        lines.append(
            "4. **本次样本下没有发现 |r|≥0.7 的高相关分项对**，暂时没有「重复计分」的直接证据——"
            "但样本量小（当前候选池数量有限），这个结论随候选池规模扩大可能会变。"
        )
    lines.append("")

    lines += [
        "## 按池子拆分（按 CompositeScore 降序）",
        "",
        "| 交易对 | NominalAPR | VTRatio | IL 风险 | CapitalVol | AgePenalty | Score | 置信度 | 分档 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    ranked = sorted(scores.items(), key=lambda kv: kv[1].score, reverse=True)
    by_address = {m.pool_address: m for m in raw_metrics}
    for pool_address, result in ranked:
        m = by_address[pool_address]
        label = pool_label(pool_address, pool_names, network)

        def _fmt(v: float | None, pct: bool = True) -> str:
            if v is None:
                return "n/a"
            return f"{v * 100:.3f}%" if pct else f"{v:.4f}"

        lines.append(
            f"| {label} | {_fmt(m.nominal_apr)} | {_fmt(m.vt_ratio, pct=False)} | "
            f"{_fmt(m.il_risk_raw)} | {_fmt(m.capital_volatility, pct=False)} | "
            f"{_fmt(m.age_penalty, pct=False)} | {result.score:.4f} | {result.confidence:.2f} | {result.tier} |"
        )

    return "\n".join(lines)


@click.command()
@click.option("--limit", type=int, default=None, help="最多校准多少个 qualified 候选池，默认全部")
@click.option(
    "--output",
    default="apps/lp-backtest/calibrate/weights_report.md",
    show_default=True,
)
def main(limit: int | None, output: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    chain = Chain.BSC
    with session_scope() as session:
        pools = PoolCandidateRepository(session).list_by_status(chain, PoolCandidateStatus.QUALIFIED)
    if limit is not None:
        pools = pools[:limit]
    pool_addresses = [row.pool_address for row in pools]

    logger.info("开始对 %d 个 qualified 候选池做权重校准", len(pool_addresses))

    adapter = build_bsc_adapter()
    plugin = PancakeswapV3Plugin()
    cake_usd_price = CoinGeckoClient().get_simple_price_usd(COINGECKO_CAKE_ID)

    gecko = GeckoTerminalClient(network=chain.value)
    pool_names = {addr: s.name for addr, s in gecko.get_pool_snapshots(pool_addresses).items() if s.name}

    raw_metrics = collect_raw_metrics(
        pools, chain=chain, adapter=adapter, plugin=plugin, cake_usd_price=cake_usd_price
    )

    normalized_by_field = normalize_all_components(raw_metrics)
    scores = compute_composite_scores(raw_metrics, normalized_by_field)

    report = _render_report(raw_metrics, normalized_by_field, scores, pool_names, chain.value)
    with open(output, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("校准完成，共 %d 个池子，报告写入 %s", len(raw_metrics), output)


if __name__ == "__main__":
    main()
