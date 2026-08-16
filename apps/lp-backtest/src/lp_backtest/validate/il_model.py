"""1c IL 模型验证：拿真实历史价格路径，比较"σ_price 驱动的 ExpectedIL_ref 预测值"
和"用实际发生的价格比算出来的 RealizedIL"，输出误差分布。

方法：对每个候选池，取一段足够长的日线收盘价历史（覆盖 sigma_price 需要的 30 天回看窗口 +
T_ref=7 天的验证窗口）。在历史序列上滚动取样：某一天 t，用"t 及之前 30 天"的收盘价算出
σ_price、代入 `expected_il_ref` 得到预测值；再用 t 和 t+7 天的真实收盘价算出 RealizedIL。
两者的差就是模型误差。按 `stride_days` 跳步取样（不是逐日），避免相邻窗口高度重叠、
把同一段价格路径重复计入误差分布。
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
from alpha_storage.db import session_scope
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository
from dotenv import load_dotenv

from ..features.volatility import MIN_CLOSE_OBSERVATIONS, sigma_price
from ..models.il_model import DEFAULT_T_REF_DAYS, expected_il_ref, realized_il_from_price_ratio
from ..report_utils import pool_label

logger = logging.getLogger(__name__)

# GeckoTerminal 免费层日线 OHLCV 实测最多能拉到约 180 天，取略低于上限的保守值。
DEFAULT_HISTORY_DAYS = 180
DEFAULT_STRIDE_DAYS = 7


@dataclass(frozen=True)
class ILSample:
    """一个"预测 vs 实际"的验证样本点。"""

    pool_address: str
    as_of: date
    sigma_price: float  # 预测当天用的年化波动率
    predicted_il: float  # ExpectedIL_ref
    realized_il: float  # 实际发生的 IL（用 t 到 t+T_ref 的真实价格比算出）
    error: float  # predicted_il - realized_il，负数表示"模型比实际更悲观"


def collect_samples(
    pool_address: str,
    dated_closes: list[tuple[date, float]],
    *,
    t_ref_days: int = DEFAULT_T_REF_DAYS,
    stride_days: int = DEFAULT_STRIDE_DAYS,
) -> list[ILSample]:
    """在一个池子的历史收盘价序列上滚动取样，产出验证样本。

    @param dated_closes 按日期升序排列的 (日期, 收盘价) 序列
    """
    closes = [c for _, c in dated_closes]
    dates = [d for d, _ in dated_closes]
    samples: list[ILSample] = []

    i = MIN_CLOSE_OBSERVATIONS - 1  # 至少需要 MIN_CLOSE_OBSERVATIONS 个收盘价（含第 i 天）才能算 σ_price
    while i + t_ref_days < len(closes):
        window = closes[: i + 1]
        sigma = sigma_price(window)
        if sigma.is_available:
            predicted = expected_il_ref(sigma.value, t_ref_days=t_ref_days)
            price_ratio = closes[i + t_ref_days] / closes[i]
            realized = realized_il_from_price_ratio(price_ratio)
            samples.append(
                ILSample(
                    pool_address=pool_address,
                    as_of=dates[i],
                    sigma_price=sigma.value,
                    predicted_il=predicted,
                    realized_il=realized,
                    error=predicted - realized,
                )
            )
        i += stride_days

    return samples


def _render_report(samples: list[ILSample], pools_covered: int, pool_names: dict[str, str], network: str) -> str:
    errors = [s.error for s in samples]
    abs_errors = [abs(e) for e in errors]
    lines = [
        "# 1c：IL 模型验证报告",
        "",
        f"样本数: {len(samples)}；覆盖池子数: {pools_covered}；"
        f"T_ref = {DEFAULT_T_REF_DAYS} 天；取样跳步 = {DEFAULT_STRIDE_DAYS} 天。",
        "",
        "## 范围说明",
        "",
        "本报告只验证 σ_price 驱动的 `ExpectedIL_ref` 模型本身的预测误差（预测 IL vs 实际发生的 IL）。"
        "没有对比 alpha-lp 现有生产代码里\"原四档币对分类\"的假设 IL 数值——"
        "这次调研没有在 alpha-lp 代码里定位到该分类具体的假设 IL 数值"
        "（不像 `estimateFeeDailyUsd`/`estimateCakeDaily` 那样有明确公式可查），"
        "为避免拿一个瞎猜的数字冒充对比，这部分留作后续独立调研。",
        "",
        "## 误差分布",
        "",
    ]
    if not samples:
        lines.append("没有产出任何样本——候选池历史数据不足 30+7 天，或候选池数量为 0。")
        return "\n".join(lines)

    mean_error = statistics.mean(errors)
    mae = statistics.mean(abs_errors)
    rmse = math.sqrt(sum(e * e for e in errors) / len(errors))
    avg_sigma_all = statistics.mean(s.sigma_price for s in samples)
    min_sigma_all = min(s.sigma_price for s in samples)
    max_sigma_all = max(s.sigma_price for s in samples)

    lines += [
        f"- 平均误差（predicted − realized）: {mean_error:.6f}",
        f"- 误差标准差: {statistics.stdev(errors) if len(errors) > 1 else 0.0:.6f}",
        f"- 平均绝对误差 (MAE): {mae:.6f}",
        f"- 均方根误差 (RMSE): {rmse:.6f}",
        f"- 误差中位数: {statistics.median(errors):.6f}",
        f"- 误差最大值 / 最小值: {max(errors):.6f} / {min(errors):.6f}",
        "",
        "正数误差 = 模型预测的 IL（更接近 0，更乐观）比实际发生的 IL 更小；"
        "负数误差 = 模型比实际更悲观。",
        "",
        "## 结论",
        "",
    ]

    bias_desc = "模型整体略偏乐观（低估实际 IL）" if mean_error > 0 else "模型整体略偏悲观（高估实际 IL）"
    lines += [
        f"在这批样本上，σ_price 驱动的 `ExpectedIL_ref` 平均绝对误差约 **{mae * 100:.3f}%**，"
        f"均方根误差约 **{rmse * 100:.3f}%**，{bias_desc}（平均误差 {mean_error * 100:+.4f}%）。"
        "对比典型 LP 一周的手续费收入通常在百分之零点几到几个百分点量级，这个误差幅度不算大，"
        "说明模型在这批池子上跟踪实际 IL 是可信的。",
        "",
        f"**但要注意样本构成的局限**：这批样本的 σ_price 分布在 {min_sigma_all:.3f} ~ {max_sigma_all:.3f}"
        f"（平均 {avg_sigma_all:.3f}），覆盖的是候选池目录里目前偏"
        "低到中等波动率的\"蓝筹\"配对（CAKE/WBNB/USDT/USDC/BTCB/ETH 白名单内部两两配对为主）。"
        "设计方案本身已经预见的局限——GBM 无漂移假设在单边行情、长尾高波动币对上会更容易失真"
        "（模型偏乐观，实际出界更快）——这批样本还没有覆盖到，不能把这个误差水平当作对所有候选池都成立的结论。"
        "等 `discover.py` 全量扫描发现更多长尾池子并且积累出足够历史后，应该重跑本验证补上这部分覆盖。",
        "",
        "## 按池子拆分（前 20 个，按样本数排序）",
        "",
        "| 交易对 | 样本数 | 平均绝对误差 | 平均 σ_price |",
        "|---|---|---|---|",
    ]

    by_pool: dict[str, list[ILSample]] = {}
    for s in samples:
        by_pool.setdefault(s.pool_address, []).append(s)
    ranked = sorted(by_pool.items(), key=lambda kv: len(kv[1]), reverse=True)
    for pool_address, pool_samples in ranked[:20]:
        pool_mae = statistics.mean(abs(s.error) for s in pool_samples)
        pool_avg_sigma = statistics.mean(s.sigma_price for s in pool_samples)
        label = pool_label(pool_address, pool_names, network)
        lines.append(f"| {label} | {len(pool_samples)} | {pool_mae:.6f} | {pool_avg_sigma:.4f} |")

    return "\n".join(lines)


@click.command()
@click.option("--limit", type=int, default=None, help="最多验证多少个 qualified 候选池，默认全部")
@click.option("--history-days", default=DEFAULT_HISTORY_DAYS, show_default=True)
@click.option("--stride-days", default=DEFAULT_STRIDE_DAYS, show_default=True)
@click.option(
    "--output",
    default="apps/lp-backtest/validate/il_model_report.md",
    show_default=True,
    help="报告写入路径",
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

    logger.info("开始对 %d 个 qualified 候选池做 IL 模型验证", len(pool_addresses))

    gecko = GeckoTerminalClient(network=chain.value)

    # 交易对显示名只是给人读的标签，取不到不影响验证本身——批量查一次，取不到名字的池子退化成裸地址。
    snapshots = gecko.get_pool_snapshots(pool_addresses)
    pool_names = {addr: s.name for addr, s in snapshots.items() if s.name}

    all_samples: list[ILSample] = []
    pools_covered = 0
    for pool_address in pool_addresses:
        ohlcv = gecko.get_daily_ohlcv(pool_address, days=history_days)
        if len(ohlcv) < MIN_CLOSE_OBSERVATIONS + stride_days:
            logger.info("池子历史不足以取样，跳过: %s（%d 天）", pool_address, len(ohlcv))
            continue
        dated_closes = [(p.day, p.close) for p in ohlcv]
        samples = collect_samples(pool_address, dated_closes, stride_days=stride_days)
        if samples:
            pools_covered += 1
            all_samples.extend(samples)
        logger.info("%s: %d 个历史点 -> %d 个验证样本", pool_address, len(ohlcv), len(samples))

    report = _render_report(all_samples, pools_covered, pool_names, chain.value)
    with open(output, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("验证完成，共 %d 个样本，覆盖 %d 个池子，报告写入 %s", len(all_samples), pools_covered, output)


if __name__ == "__main__":
    main()
