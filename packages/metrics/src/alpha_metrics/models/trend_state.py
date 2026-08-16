"""趋势/波动率状态模型，阶段 1 新增（`live-signal-system-设计方案.md` 第 5 章）。基于
`features.adx`/`features.atr` 的简单状态机，决定用 `recommended_range.MAINTENANCE_PROFILES`
里哪一档更合适——不是新公式，是"选哪个已有推荐档位"的规则。

**趋势分档（3 档，不是设计文档字面的二分"强/弱"）**：ADX 20-25 之间是业界公认的模糊区间，
不硬按单一切点二分（那样会把这个本来就有争议的区间强行划给某一边），改成独立的"过渡"档，
统一落到"平衡型"，不赌方向——跟设计文档字面的"四象限"有出入，这是一次确认过的调整。

**波动分档**：不用固定绝对阈值——同一个 ATR% 数字对不同池子、不同市场环境意味不一样
（这跟 `models.normalize.normalize_min_max`"相对候选集/自身历史，不用绝对阈值"是同一个哲学）。
改成跟这个池子自己最近 `ATR_PCT_HISTORY_WINDOW` 根 5 分钟K线的 ATR% 中位数比：高于中位数算
"高波动"，反之"低波动"。

6 格映射表（3 档趋势 × 2 档波动）：

| ADX 趋势 | ATR% 波动 | 推荐档位 | 理由 |
|---|---|---|---|
| 强 | 高 | 被动型(30天) | 设计文档："强趋势应该放宽区间或直接退出" |
| 强 | 低 | 平衡型(7天) | 趋势明确但波动不大，不用最宽也不用最窄 |
| 过渡 | 高/低 | 平衡型(7天) | 趋势不明确，不赌方向 |
| 弱 | 高 | 平衡型(7天) | 震荡但波动大，窄区间容易来回被扫 |
| 弱 | 低 | 主动型(3天) | 设计文档："震荡市适合窄区间吃手续费" |
"""

from __future__ import annotations

import statistics

from alpha_core.metrics import MetricValue

from .recommended_range import MAINTENANCE_PROFILES

ADX_STRONG_TREND_THRESHOLD = 25.0
ADX_WEAK_TREND_THRESHOLD = 20.0
ATR_PCT_HISTORY_WINDOW = 30  # 滚动窗口根数，用来算"这个池子自己的" ATR% 中位数基准

_ACTIVE, _BALANCED, _PASSIVE = "主动型", "平衡型", "被动型"

_PROFILE_TABLE: dict[tuple[str, str], str] = {
    ("strong", "high"): _PASSIVE,
    ("strong", "low"): _BALANCED,
    ("transitional", "high"): _BALANCED,
    ("transitional", "low"): _BALANCED,
    ("weak", "high"): _BALANCED,
    ("weak", "low"): _ACTIVE,
}


def classify_trend(adx: float) -> str:
    """@returns "strong"（ADX > 25）/ "weak"（ADX < 20）/ "transitional"（20-25 之间）"""
    if adx > ADX_STRONG_TREND_THRESHOLD:
        return "strong"
    if adx < ADX_WEAK_TREND_THRESHOLD:
        return "weak"
    return "transitional"


def classify_volatility(atr_pct_now: float, atr_pct_history: list[float]) -> str:
    """@param atr_pct_history 这个池子自己最近 `ATR_PCT_HISTORY_WINDOW` 根K线各自的 ATR%
    （不含当前这一根），用来算中位数基准——历史为空时无法比较，统一归为"low"（没有历史就
    不该轻举妄动，跟"数据不足就保守"是同一个原则，但这里不是三态 unavailable，因为
    调用方 `select_maintenance_profile` 已经在更上一层处理"输入不可用"的情形）。
    @returns "high"（高于自身历史中位数）/ "low"（低于或等于）
    """
    if not atr_pct_history:
        return "low"
    return "high" if atr_pct_now > statistics.median(atr_pct_history) else "low"


def select_maintenance_profile(
    adx: MetricValue[float], atr_pct: MetricValue[float], atr_pct_history: list[float]
) -> MetricValue[str]:
    """@returns `MAINTENANCE_PROFILES` 的一个 key（"主动型"/"平衡型"/"被动型"）。纯函数，
    不做任何 I/O——上层负责把最近 `ATR_PCT_HISTORY_WINDOW` 根K线各自的 ATR% 算好传进来。
    `adx`/`atr_pct` 任一不可用 → 整体不可用，不能在信息不全的情况下悄悄选一个档位。
    """
    if not adx.is_available:
        return MetricValue.unavailable(f"ADX 不可用：{adx.reason}")
    if not atr_pct.is_available:
        return MetricValue.unavailable(f"ATR% 不可用：{atr_pct.reason}")

    trend = classify_trend(adx.value)
    volatility = classify_volatility(atr_pct.value, atr_pct_history)
    profile = _PROFILE_TABLE[(trend, volatility)]
    assert profile in MAINTENANCE_PROFILES  # 映射表的 value 必须是合法的档位 key，防止拼写漂移
    return MetricValue.available(profile)
