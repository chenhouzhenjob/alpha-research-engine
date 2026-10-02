"""候选集内 min-max 归一化（pool-discovery-metrics-v1.md 2.1 节）。

"什么算高 APR"会随市场整体行情漂移，相对排名比绝对值更稳定，所以复合分不用固定绝对阈值，
而是在**当前候选集内**做 min-max 归一化到 [0,1]。
"""

from __future__ import annotations


def normalize_min_max(values: list[float]) -> list[float]:
    """@returns 归一化后的值，与输入等长、顺序一致；全部相同（或只有一个值）时统一返回 0.5——
    不能除零，也不能武断给 0 或 1（那等于说"和别人比是最低/最高"，但根本没有可比的差异）。
    """
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]
