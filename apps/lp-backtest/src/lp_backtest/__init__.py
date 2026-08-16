"""lp-backtest：LP 池发现指标回归系统。

1a 阶段范围：候选池发现（Factory 全量扫描）+ 历史数据接入（GeckoTerminal）。
指标计算/IL 模型验证/区间回测/权重校准（1b-1e）在此数据基础上展开，见
research/docs/lp-backtest-设计方案-v1.md 第 4 章执行计划。
"""
