"""1f 产物输出：把 `alpha_metrics.models.composite_score` 里的真实权重/阈值常量导出成
alpha-lp 可消费的版本化 JSON 配置。

**只导出代码里已经在用的常量，不在这里重新抄一遍数字**——权重/阈值只有一份来源
（`composite_score.py`），避免这份 JSON 和代码实际使用的值不同步。

**这份配置目前的校准状态是"未校准"**：权重是 pool-discovery-metrics-v1.md 的首版经验值，
1e 阶段（`calibrate/weights.py`）跑真实数据发现了几个值得处理的问题（详见
`calibrate/weights_report.md`），但校准报告只给了"建议"，没有直接改这里的数字——
改权重是需要 alpha-lp 团队拍板的产品决策，不是这次分析自己该定的，所以 `calibration_status`
字段如实写 `not_calibrated`，`known_issues` 里列出 1e 报告发现的具体问题，供人工评审参考。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import click
from alpha_metrics.models.composite_score import AGE_PENALTY_WEIGHT, NORMALIZED_COMPONENT_WEIGHTS, TIER_THRESHOLDS

logger = logging.getLogger(__name__)

CONFIG_VERSION = "1.0.0"

KNOWN_ISSUES = [
    "理论可达分数范围（约 [-0.45, 0.55]，未考虑数据缺失重新分摊）和 S/A/B/C 阈值（S≥0.75）不匹配，"
    "S 档在当前公式结构下几乎不可能触发——需要重新标定阈值，或改公式让加分项权重合计归一到 1。",
    "AgePenalty 不参与归一化，原始值只有 0/0.05，乘 10% 权重后最大实际影响约 0.005，"
    "和其名义 10% 权重不成比例。",
    "CapitalVolatility 依赖 14 天真实 TVL 历史，而 GeckoTerminal 免费层不提供历史 TVL 时间序列"
    "（见 1a/1c 已记录的限制），当前候选池目录里这一项的覆盖率结构性偏低，"
    "对应权重被系统性重新分摊给其余分项，不是这一项本身没有校准价值。",
    "ExpectedIL_ref 恒为负数，复合分公式对它的归一化改用绝对值（|ExpectedIL_ref|），"
    "这是本次实现对原文档公式的必要解读，不是原文档明确写出的符号约定，"
    "接入前需要 alpha-lp 团队确认这个解读符合预期。",
]


def build_config() -> dict:
    return {
        "config_version": CONFIG_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "chain": "bsc",
        "protocol": "pancakeswap-v3",
        "source_design_doc": "research/docs/pool-discovery-metrics-v1.md",
        "calibration_status": "not_calibrated",
        "composite_score": {
            "normalized_components": {
                name: {"weight": weight, "direction": "positive" if sign > 0 else "negative"}
                for name, (weight, sign) in NORMALIZED_COMPONENT_WEIGHTS.items()
            },
            "age_penalty": {
                "weight": AGE_PENALTY_WEIGHT,
                "normalized": False,
                "note": "原始值 0 或 0.05，不参与其余分项的 min-max 归一化，也不参与数据缺失时的权重重新分摊",
            },
        },
        "tiers": [{"tier": tier, "min_score": threshold} for tier, threshold in TIER_THRESHOLDS]
        + [{"tier": "C", "min_score": None}],
        "known_issues": KNOWN_ISSUES,
        "source_reports": {
            "il_model_validation": "apps/lp-backtest/validate/il_model_report.md",
            "range_backtest": "apps/lp-backtest/validate/range_backtest_report.md",
            "weight_calibration": "apps/lp-backtest/calibrate/weights_report.md",
        },
    }


@click.command()
@click.option(
    "--output",
    default=f"apps/lp-backtest/config/composite_score_weights.v{CONFIG_VERSION.split('.')[0]}.json",
    show_default=True,
)
def main(output: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = build_config()
    with open(output, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("配置已导出: %s", output)


if __name__ == "__main__":
    main()
