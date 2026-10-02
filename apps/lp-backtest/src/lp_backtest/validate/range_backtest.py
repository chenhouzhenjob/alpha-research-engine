"""1d 推荐区间回测：验证 3/7/30 天三档推荐区间（pool-discovery-metrics-v1.md 1.4 节）在
真实历史价格路径下的表现——实际出界时间、（资金效率意义上的）手续费捕获、IL，和模型预测对比。

**手续费捕获的范围说明（务必先读）**：GeckoTerminal 免费层不提供历史每日 volume（见 1a
README 已记录的限制），没法把"某天的真实手续费收入"按历史逐日重算。这里只回测"资金效率利用率"
这个纯粹由真实历史价格路径决定、不依赖历史 volume 的量——区间在目标周期内有多大比例的时间
价格真的留在区间里（`utilization_ratio`），乘上集中流动性相对全范围的资金效率倍数
（`capital_efficiency`，见 `alpha_metrics.models.recommended_range`）。这是一个"资金效率有没有兑现"的
真实历史回测，不是"这段时间赚了多少美元"的回测——后者需要历史 volume，目前拿不到，
不能用"今天的费率套用到历史每一天"这种做法冒充历史回测，那样会把 volume 随价格波动的真实变化抹平。
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import date

import click
from alpha_core.types import Chain, PoolCandidateStatus
from alpha_datasources.geckoterminal import GeckoTerminalClient
from alpha_metrics.features.volatility import MIN_CLOSE_OBSERVATIONS, sigma_price
from alpha_metrics.models.il_model import expected_il_ref, realized_il_from_price_ratio
from alpha_metrics.models.recommended_range import MAINTENANCE_PROFILES, capital_efficiency, recommended_width
from alpha_storage.db import session_scope
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository
from dotenv import load_dotenv

from ..report_utils import pool_label
from .il_model import DEFAULT_HISTORY_DAYS

logger = logging.getLogger(__name__)

DEFAULT_STRIDE_DAYS = 7
DEFAULT_EXTENDED_LOOKAHEAD_DAYS = 90


@dataclass(frozen=True)
class RangeSample:
    """某个池子、某一天、某一档维护画像的推荐区间回测样本。"""

    pool_address: str
    as_of: date
    profile: str
    t_target_days: int
    sigma_price: float
    width: float  # 区间半宽（log 价格）
    breached_within_target: bool  # 目标周期内是否真的出界了
    actual_survival_days: int | None  # 实际首次出界用了多少天；None = 扩展窗口内都没出界（删失）
    utilization_ratio: float  # 目标周期内价格留在区间里的时间占比，0~1
    capital_efficiency: float  # 集中流动性相对全范围的资金效率倍数
    predicted_il: float
    realized_il: float | None  # None = 历史数据不够，算不出 t+T_target 那天的真实 IL
    il_error: float | None  # predicted - realized；None 同上


def _first_breach_offset(closes: list[float], start_idx: int, width: float, max_offset: int) -> int | None:
    """从 `start_idx` 往后最多看 `max_offset` 天，价格首次超出 [P₀e⁻ʷ, P₀eʷ] 的偏移天数。

    @returns 未在窗口内突破（含数据不够看到窗口尽头）时返回 None（删失）
    """
    p0 = closes[start_idx]
    lower, upper = p0 * math.exp(-width), p0 * math.exp(width)
    for offset in range(1, max_offset + 1):
        idx = start_idx + offset
        if idx >= len(closes):
            return None
        if closes[idx] < lower or closes[idx] > upper:
            return offset
    return None


def collect_range_samples(
    pool_address: str,
    dated_closes: list[tuple[date, float]],
    *,
    stride_days: int = DEFAULT_STRIDE_DAYS,
    extended_lookahead_days: int = DEFAULT_EXTENDED_LOOKAHEAD_DAYS,
) -> list[RangeSample]:
    """在一个池子的历史收盘价序列上滚动取样，对三档维护画像分别回测。"""
    closes = [c for _, c in dated_closes]
    dates = [d for d, _ in dated_closes]
    samples: list[RangeSample] = []

    i = MIN_CLOSE_OBSERVATIONS - 1
    while i < len(closes):
        sigma = sigma_price(closes[: i + 1])
        if sigma.is_available:
            for profile, t_target in MAINTENANCE_PROFILES.items():
                width = recommended_width(sigma.value, t_target)
                breach_offset_in_target = _first_breach_offset(closes, i, width, t_target)
                actual_survival = _first_breach_offset(closes, i, width, extended_lookahead_days)
                utilization = (
                    breach_offset_in_target if breach_offset_in_target is not None else t_target
                ) / t_target

                predicted = expected_il_ref(sigma.value, t_ref_days=t_target)
                realized = None
                il_error = None
                if i + t_target < len(closes):
                    price_ratio = closes[i + t_target] / closes[i]
                    realized = realized_il_from_price_ratio(price_ratio)
                    il_error = predicted - realized

                samples.append(
                    RangeSample(
                        pool_address=pool_address,
                        as_of=dates[i],
                        profile=profile,
                        t_target_days=t_target,
                        sigma_price=sigma.value,
                        width=width,
                        breached_within_target=breach_offset_in_target is not None,
                        actual_survival_days=actual_survival,
                        utilization_ratio=utilization,
                        capital_efficiency=capital_efficiency(width),
                        predicted_il=predicted,
                        realized_il=realized,
                        il_error=il_error,
                    )
                )
        i += stride_days

    return samples


def _render_profile_section(profile: str, t_target: int, samples: list[RangeSample]) -> list[str]:
    lines = [f"### {profile}（T_target = {t_target} 天）", "", f"样本数: {len(samples)}", ""]
    if not samples:
        lines.append("没有样本。")
        return lines

    breach_rate = sum(1 for s in samples if s.breached_within_target) / len(samples)
    mean_utilization = statistics.mean(s.utilization_ratio for s in samples)
    mean_efficiency = statistics.mean(s.capital_efficiency for s in samples)
    il_samples = [s for s in samples if s.il_error is not None]

    lines += [
        f"- 目标周期内实际出界的比例: {breach_rate * 100:.1f}%",
        f"- 平均资金利用率（留在区间内时间占比）: {mean_utilization * 100:.1f}%",
        f"- 平均资金效率倍数（相对全范围做市）: {mean_efficiency:.2f}x",
    ]
    if il_samples:
        errors = [s.il_error for s in il_samples]
        mae = statistics.mean(abs(e) for e in errors)
        rmse = math.sqrt(sum(e * e for e in errors) / len(errors))
        lines += [
            f"- IL 预测 MAE: {mae * 100:.4f}%（{len(il_samples)} 个可比样本）",
            f"- IL 预测 RMSE: {rmse * 100:.4f}%",
        ]
    lines.append("")
    return lines


def _render_report(all_samples: list[RangeSample], pools_covered: int, pool_names: dict[str, str], network: str) -> str:
    lines = [
        "# 1d：推荐区间回测报告",
        "",
        f"覆盖池子数: {pools_covered}；总样本数: {len(all_samples)}（3 档画像共用同一批取样时间点）。",
        "",
        "## 范围说明",
        "",
        "手续费捕获只回测「资金效率利用率」（真实历史价格路径决定，不依赖历史 volume），"
        "不是「历史每天赚了多少美元」——后者需要 GeckoTerminal 免费层拿不到的历史 volume 数据，"
        "详见本文件模块 docstring。",
        "",
        "## 分画像结果",
        "",
    ]

    by_profile: dict[str, list[RangeSample]] = {}
    for s in all_samples:
        by_profile.setdefault(s.profile, []).append(s)

    for profile, t_target in MAINTENANCE_PROFILES.items():
        lines += _render_profile_section(profile, t_target, by_profile.get(profile, []))

    lines += [
        "## 结论",
        "",
    ]
    if all_samples:
        breach_rates = {
            profile: sum(1 for s in by_profile.get(profile, []) if s.breached_within_target)
            / max(len(by_profile.get(profile, [])), 1)
            for profile in MAINTENANCE_PROFILES
        }
        ordered = sorted(MAINTENANCE_PROFILES.items(), key=lambda kv: kv[1])
        rate_desc = "、".join(f"{p}({breach_rates[p] * 100:.0f}%)" for p, _ in ordered)
        lines += [
            f"三档目标周期内的实际出界比例分别是 {rate_desc}。"
            "设计方案本身预见的局限是：真实价格有趋势，单边行情下实际出界速度会比模型快，模型偏乐观——"
            "如果这里看到的出界比例明显高于「一半左右」这个 GBM 直觉基准（区间宽度本来就是按 1 个标准差设的，"
            "到期时出界属于正常概率事件，不是模型失败），说明这批池子近期确实偏单边走势，"
            "该三档推荐区间在维护频率上可能需要比设计值更保守（更频繁跳仓）才能达到预期的资金利用率。",
            "",
            "同样受 1c 报告已经指出的样本构成限制：这批池子目前以白名单内部的蓝筹配对为主，"
            "波动率整体中等偏低，长尾高波动池子的表现还没有被这次回测覆盖到。",
        ]
    else:
        lines.append("没有产出任何样本。")

    lines += [
        "",
        "## 按池子拆分（平衡型 7 天档，前 20 个，按样本数排序）",
        "",
        "| 交易对 | 样本数 | 出界比例 | 平均资金利用率 | IL 预测 MAE |",
        "|---|---|---|---|---|",
    ]
    balanced_samples = [s for s in all_samples if s.profile == "平衡型"]
    by_pool: dict[str, list[RangeSample]] = {}
    for s in balanced_samples:
        by_pool.setdefault(s.pool_address, []).append(s)
    ranked = sorted(by_pool.items(), key=lambda kv: len(kv[1]), reverse=True)
    for pool_address, pool_samples in ranked[:20]:
        breach_rate = sum(1 for s in pool_samples if s.breached_within_target) / len(pool_samples)
        mean_util = statistics.mean(s.utilization_ratio for s in pool_samples)
        il_samples = [s for s in pool_samples if s.il_error is not None]
        mae_str = f"{statistics.mean(abs(s.il_error) for s in il_samples) * 100:.4f}%" if il_samples else "n/a"
        label = pool_label(pool_address, pool_names, network)
        lines.append(
            f"| {label} | {len(pool_samples)} | {breach_rate * 100:.1f}% | {mean_util * 100:.1f}% | {mae_str} |"
        )

    return "\n".join(lines)


@click.command()
@click.option("--limit", type=int, default=None, help="最多验证多少个 qualified 候选池，默认全部")
@click.option("--history-days", default=DEFAULT_HISTORY_DAYS, show_default=True)
@click.option("--stride-days", default=DEFAULT_STRIDE_DAYS, show_default=True)
@click.option(
    "--output",
    default="apps/lp-backtest/validate/range_backtest_report.md",
    show_default=True,
)
def main(limit: int | None, history_days: int, stride_days: int, output: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    chain = Chain.BSC
    with session_scope() as session:
        pools = PoolCandidateRepository(session).list_by_status(chain, PoolCandidateStatus.QUALIFIED)
    pool_addresses = [row.pool_address for row in pools]
    if limit is not None:
        pool_addresses = pool_addresses[:limit]

    logger.info("开始对 %d 个 qualified 候选池做推荐区间回测", len(pool_addresses))

    gecko = GeckoTerminalClient(network=chain.value)
    snapshots = gecko.get_pool_snapshots(pool_addresses)
    pool_names = {addr: s.name for addr, s in snapshots.items() if s.name}

    all_samples: list[RangeSample] = []
    pools_covered = 0
    for pool_address in pool_addresses:
        ohlcv = gecko.get_daily_ohlcv(pool_address, days=history_days)
        if len(ohlcv) < MIN_CLOSE_OBSERVATIONS:
            logger.info("池子历史不足以取样，跳过: %s（%d 天）", pool_address, len(ohlcv))
            continue
        dated_closes = [(p.day, p.close) for p in ohlcv]
        samples = collect_range_samples(pool_address, dated_closes, stride_days=stride_days)
        if samples:
            pools_covered += 1
            all_samples.extend(samples)
        logger.info("%s: %d 个历史点 -> %d 个回测样本", pool_address, len(ohlcv), len(samples))

    report = _render_report(all_samples, pools_covered, pool_names, chain.value)
    with open(output, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("回测完成，共 %d 个样本，覆盖 %d 个池子，报告写入 %s", len(all_samples), pools_covered, output)


if __name__ == "__main__":
    main()
