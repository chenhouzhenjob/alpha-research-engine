"""策略将市场数据与特征转换为目标权重。"""

from alpha.strategies.builtin import MovingAverageCrossStrategy
from alpha.strategies.core import Strategy, validate_target_weights

__all__ = ["MovingAverageCrossStrategy", "Strategy", "validate_target_weights"]
