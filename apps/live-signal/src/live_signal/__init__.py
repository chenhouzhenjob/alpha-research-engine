"""live-signal：实时研究驱动决策系统 · 阶段 0 的只读结论 HTTP 服务。

暴露 `GET /conclusion?chain=bsc&pool=0x...`，alpha-lp 定时拉取这个端点获得决策结论——
决策/执行逻辑不在这里，这一层只负责把 research 侧算出的信号以稳定的 HTTP 接口暴露出去。
见 research/docs/live-signal-system-设计方案.md 第 6 章"结论契约设计"。
"""
