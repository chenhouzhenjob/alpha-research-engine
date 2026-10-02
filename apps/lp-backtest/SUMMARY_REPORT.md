# lp-backtest 汇总报告（1c / 1d / 1e）

对应 [`../../docs/lp-backtest-设计方案-v1.md`](../../docs/lp-backtest-设计方案-v1.md) 第 4 章 1f
"产物输出"的验收标准之一："汇总报告（含 1c/1d/1e 结论）"。三份详细报告分别是：

- 1c：[`validate/il_model_report.md`](validate/il_model_report.md)
- 1d：[`validate/range_backtest_report.md`](validate/range_backtest_report.md)
- 1e：[`calibrate/weights_report.md`](calibrate/weights_report.md)

配套的版本化权重配置：[`config/composite_score_weights.v1.json`](config/composite_score_weights.v1.json)
（由 [`config_export.py`](src/lp_backtest/config_export.py) 从代码里的真实常量导出，不是手抄的数字）。

## 一句话结论

σ_price 驱动的 IL 模型本身预测得相当准（1c/1d 都验证过），但复合打分公式（1.2 节 CompositeScore）
的权重和分档阈值目前对不上——真实数据显示分数几乎不可能进 S 档，这是 1e 最重要的发现，
建议 alpha-lp 团队在接入生产前先决定怎么处理这个问题（详见下方"需要人工决策的事项"）。

## 1c：IL 模型验证

- 27 个 qualified 候选池、567 个样本：预测 IL 平均绝对误差 0.028%，RMSE 0.043%。
- 模型在这批池子（以白名单蓝筹配对为主，σ_price 0.003~0.72）上是可信的。
- **没有覆盖**：长尾高波动池子（还没被全量扫描发现进候选池目录），也没有对比设计方案提到的
  "原四档币对分类"（没在 alpha-lp 代码里找到那个分类的具体假设数值）。

## 1d：推荐区间回测

- 主动型(3天)/平衡型(7天)/被动型(30天) 三档：出界比例 28%/34%/38%，资金利用率 91%/87%/82%。
- IL 预测 MAE 随周期变长而变大（0.015%/0.028%/0.174%），符合"更长周期更难预测"的直觉。
- 手续费捕获只回测了"资金效率利用率"（价格留在区间内的真实历史比例），不是"历史每天赚了多少美元"——
  后者需要 GeckoTerminal 免费层拿不到的历史 volume 数据，1a 阶段就有这个已知限制。

## 1e：权重与分档校准 —— 最重要的发现在这里

用 27 个候选池的当前指标跑了一遍默认权重（0.30/0.25/0.25/0.10/0.10），四个具体发现：

1. **分数范围和分档阈值对不上**：理论上限约 0.55（扣分项都为 0 时），S 档阈值是 0.75，
   结构上几乎不可能达到。真实数据跑出来的分数范围是 -0.23 ~ 0.61，印证了这一点——
   27 个池子里 0 个 S、1 个 A（4%）、0 个 B、26 个 C（96%）。分档形同虚设。
2. **CapitalVolatility 当前 100% 不可得**：不是这个指标没用，是 TVL 历史数据还没积累够 14 天
   （GeckoTerminal 不提供历史 TVL，只能靠 `ingest` 逐日积累），这一项的 10% 权重目前全部
   重新分摊给其余四项。
3. **AgePenalty 名义权重（10%）和实际影响力（最大 0.005）不成比例**，因为它不参与归一化。
4. **NominalAPR 和 VTRatio 高度相关**（r=0.79）——两者可能在重复计分同一个"这个池子活跃度高"的信号。

## 需要人工决策的事项（alpha-lp 团队评审时请重点看这几条）

1. **CompositeScore 公式结构要不要改**：是重新标定 S/A/B/C 阈值去匹配当前公式的实际分数范围，
   还是改公式结构（让加分项权重合计归一到 1，扣分项作为折扣而不是独立减项）？
   两个方案改动面和含义都不一样，`calibrate/weights_report.md` 第 1 条有详细对比，需要产品侧拍板。
2. **ExpectedIL_ref 的符号处理**：本次实现把复合分公式里的 `normalize(ExpectedIL_ref)` 解读成
   `normalize(|ExpectedIL_ref|)`（原文档没有明确写符号约定，直接对负数归一化会导致风险最低的池子
   被扣最多，方向反了）。这个解读需要 alpha-lp 团队确认符合设计意图。
3. **NominalAPR/VTRatio 相关性**：要不要在权重里体现"这两项有重复"，比如降低其中一项权重。
4. **配置格式本身**（`config/composite_score_weights.v1.json`）：alpha-lp 团队确认这份 JSON 结构
   可以直接被生产实现消费——这是设计方案 1f 验收标准明确要求的人工确认步骤，本次实现只能产出格式本身，
   没法替团队做这个确认。

## 已知技术债（汇总，明细见各阶段 README/报告）

- CAKE emission、feeProtocol、CAKE/USD 价格都是"调用时刻的当前值"，历史日期的 FeeAPR/CakeAPR
  本质是"用当前费率套用到那一天的 volume/TVL"，不是严格历史重现（1b）。
- TVL/24h volume 没有历史时间序列，只能逐日 ingest 积累（1a，直接导致 1e 的 CapitalVolatility 覆盖率问题）。
- Python 是一次性引导实现，等 alpha-lp 把这套指标计算做成生产纯函数模块后需要切换成跨进程调用真实实现，
  这里的 Python 版本退役（1a/1b 已记录）。
- 候选池目录目前以白名单蓝筹配对为主，长尾池子覆盖不足，1c/1d/1e 的所有结论都只在"当前这批候选池"上成立，
  跑完 `discover.py` 全量扫描、积累更多历史后应该重跑一遍。
