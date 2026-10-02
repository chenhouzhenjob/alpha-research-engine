"""alpha_datasources：外部数据源接入层，按优先级降级使用。

本期（lp-backtest 1a）只接入 GeckoTerminal 一个来源；
钱包链上行为分析设计方案里的 Subgraph/推送/RPC 降级链留到有实际需求时再补，
不提前搭一套没有调用方的编排器。
"""
