"""alpha_metrics：特征层（`features/`）+ 模型层（`models/`）+ 组装层（`compute.py`），
跨 `apps/lp-backtest`、`apps/live-signal` 共享，不属于任何一个 app 私有。

- `features/`：从原始数据做确定性变换，不预测、不建议。
- `models/`：引入模型假设做预测，或综合多个特征给出打分/建议。
- `compute.py`：把两层拼成一个池子/日期的完整信号包。
- `chain_reads.py`：`features`/`compute` 需要的少量链上"当前值"读取（feeProtocol/CAKE 排放）。
"""
