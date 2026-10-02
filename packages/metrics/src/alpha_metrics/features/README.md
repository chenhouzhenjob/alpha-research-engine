# features/ 指标说明文档真相

这个目录下每个指标的**唯一权威定义**在这个文件里——代码改动（公式、阈值、三态判定条件）必须
在同一次改动里同步这个文档，跟 [`../models/README.md`](../models/README.md) 之于模型层、
[`SCHEMA.md`](../../../../../SCHEMA.md) 之于表结构是同一个约定（维护约定的完整说明见
[`../models/README.md`](../models/README.md) 最后一节）。

## 0. 前置：`MetricValue` 三态返回值

每个指标函数都返回 [`alpha_core.metrics.MetricValue`](../../../../core/src/alpha_core/metrics.py)，
不是裸的 `float | None`——三种状态（`alpha_core.types.MetricAvailability`）：

| 状态 | 含义 | `value` |
|---|---|---|
| `AVAILABLE` | 算出来了 | 有值 |
| `NO_INCENTIVE` | **确认过**是真的没有激励（比如这个池子确实没有 CAKE farm），不是数据缺失 | 恒为 `None` |
| `UNAVAILABLE` | 数据不够/取不到，算不出来 | 恒为 `None` |

`__post_init__` 强制这个约束（`AVAILABLE` 必须有值，其余两种必须是 `None`），防止"偷偷带着一个值
却标记成不可用"这类静默 bug。调用方（打分、序列化、报告渲染）必须显式分支处理三种状态，
不能把"缺数据"和"确认为零"混为一谈去当 0 处理——这是从 alpha-lp 的 `AprEstimateView` 沿用过来的
约定，见 [`alpha_core/metrics.py`](../../../../core/src/alpha_core/metrics.py) 的完整文档。

---

## 1. 收益类指标

### FeeAPR
[`fee_apr.py`](fee_apr.py)

```
lp_net_share = 1 - (feeProtocol0 + feeProtocol1) / 2 / 10000
daily_fee_usd = volume_24h_usd × fee_pips/1e6 × lp_net_share
FeeAPR = daily_fee_usd / tvl_usd × 365
```

池子整体的年化手续费收益率（不是某个具体仓位按流动性份额分到的那一份——设计上就是
"池子级别"指标，不是"仓位级别"，跟 alpha-lp 的 `estimateFeeDailyUsd` 是同一个逻辑，
去掉了仓位的 `liquidityShare` 因子）。`feeProtocol0/1` 是协议抽成比例（`slot0()` 读到，
单位 1/10000），来自 `read_fee_protocol`。

**不可用**：`volume_24h_usd`/`tvl_usd` 缺失，或 `tvl_usd <= 0`。

### CakeAPR
[`cake_apr.py`](cake_apr.py)

```
CakeAPR = cakePerSecond × 86400 × lmLiquidityShare × CAKE_USD价格 / tvl_usd × 365
```

年化 CAKE 挖矿激励收益率。`lmLiquidityShare` 是当前池子活跃流动性里质押进 MasterChef 的比例
（池子级别，不是仓位级别）。

**已知局限（不是实现缺陷，是链上数据本身的限制）**：CAKE 排放速率会随治理投票周期性调整，
但链上（包括 alpha-lp 和 MasterChef V3）都只暴露"当前这一刻"的排放速率快照，没有历史排放
事件日志可以重建历史——所以这是"用当前排放率静态外推 365 天"，不是"未来 365 天排放率不变"
的保证。

**三态**：`cake_emission is None`（这个池子确实没有 farm）→ `NO_INCENTIVE`，不是
`UNAVAILABLE`；CAKE/USD 价格或 TVL 缺失/非正 → `UNAVAILABLE`。

### NominalAPR
[`nominal_apr.py`](nominal_apr.py)

```
NominalAPR = FeeAPR + CakeAPR
```

最直观但也最容易误导人的数字——"如果什么都不变"的理论年化收益，没有做任何风险调整，
**不能单独拿来排名**，只能作为复合打分的一个输入项。

**三态合并规则**（重要）：`CakeAPR` 是 `NO_INCENTIVE`（确认没有激励）→ 当 0 处理正常相加；
`FeeAPR`/`CakeAPR` 任一是 `UNAVAILABLE`（取不到，不是确认为零）→ 整个 `NominalAPR` 标记
`UNAVAILABLE`，不能悄悄当 0 求和——那样会把"数据缺失"伪装成"确认无收益"。

### VTRatio（Volume/TVL Ratio）
[`vt_ratio.py`](vt_ratio.py)

```
VTRatio = volume_24h_usd / tvl_usd
```

比 APR 更早期、更可靠的信号——TVL 被低估、APR 被虚高的池子，这里会先露出马脚。建议当作
候选池筛选的**第一道过滤器**用，不只是复合打分里埋着的一个加权项。

**不可用**：volume/TVL 缺失，或 `tvl_usd <= 0`。

**`vt_ratio_ma(daily_ratios, window=7)`**（阶段 1 新增，退出信号 3"交易量枯竭"用）：

```
VTRatio_MA(N日) = mean(最近 N 天各自的 VTRatio)
```

默认 7 日均值——用均值而不是单日值，避免被短期噪音（如周末交易量正常回落）误触发。
`daily_ratios` 由调用方从 `pool_metrics_history` 逐日算好传入，这里不查数据库。

**不可用**：`daily_ratios` 长度不足 `window` 天。

---

## 2. 风险/波动率类指标

### σ_price（sigma_price，年化价格波动率）
[`volatility.py`](volatility.py)

```
σ_price = stdev(ln(closeₜ / closeₜ₋₁)) × √365
```

`MIN_CLOSE_OBSERVATIONS = 30`（30 个收盘价 → 29 个日对数收益率观测）。这是下游三个指标
（IL 风险、推荐区间、维护频率）共用的唯一输入——三者都基于同一个 GBM 近似，保证方法论一致。

**取样规则**：传入的 `closes` 可以比回看窗口长，函数只取**末尾**30 个——是"回看窗口"，
不是"把全部历史都算进方差"。

**不可用**：收盘价不足 30 个（reason 里会写具体数量），或窗口内有收盘价 `<= 0`。

### CapitalVolatility
[`capital_volatility.py`](capital_volatility.py)

```
CapitalVolatility = stdev(近14日 TVL 日环比变化率)
```

`MIN_TVL_OBSERVATIONS = 14`（14 个 TVL 观测 → 13 个环比变化率）。跟 σ_price 是不同维度：
这个反映"大户资金进出这个池子有多频繁、池子本身有多脆弱"，σ_price 反映"LP 仓位本身会不会
吃 IL"。**故意不年化**（没有 ×√365）——这是短周期稳定性信号，不是拿来定价风险用的。

**不可用**：TVL 观测不足 14 个，或有观测值 `<= 0`。已知局限（继承自 1a）：TVL 历史要逐日
采集才能积累，无法一次性回填。

### AgePenalty
[`age_penalty.py`](age_penalty.py)

```
AgePenalty = 池龄 < 30 天 ? 0.05 : 0
```

新池子数据不稳定、也是钓鱼/诈骗池子的高发区，用固定罚分压低排名而不是直接排除（准入门槛已经
过滤掉 <7 天的池子，这个是对 7-30 天这个区间的进一步降权）。

**不可用**：`created_at` 未知。

### DepthTier
[`depth_tier.py`](depth_tier.py)

```
TVL < $100k  -> HIGH_RISK
TVL < $1M    -> MEDIUM
其余          -> LOW_RISK
```

浅池子表面 APR 看起来很高，但吃不下多少资金、退出滑点也大——这是一个**独立展示标签**，
回答"这个 APR 我实际能吃到多少"，**不参与**复合打分公式。

**不可用**：`tvl_usd` 未知。

### ATR（Average True Range，真实波幅）
[`atr.py`](atr.py)

```
TR_t = max(high_t - low_t, |high_t - close_{t-1}|, |low_t - close_{t-1}|)
ATR_1 = mean(TR_1..TR_N)                       （首个值：前 N 个 TR 的简单平均）
ATR_t = (ATR_{t-1} * (N-1) + TR_t) / N          （之后：Wilder 平滑）
```

`DEFAULT_PERIOD = 14`（Wilder 惯用值，设计文档没给具体数字，这是默认假设，不是文档明确要求）。
用 5 分钟K线（`_ohlcv_window.resample_to_5m` 重采样自落库的 1 分钟K线），不用 1 分钟——
1 分钟粒度会被链上出块噪音主导，对做市调仓这种日级别的决策没有意义。跟 `σ_price`
（基于日线收盘价对数收益率）是两个不同粒度的波动率信号，互补不替代——σ_price 覆盖"慢"的
一端，ATR 覆盖"快"的一端。

**不可用**：重采样后 5 分钟K线不足 `period+1` 根。

**`atr_pct_series(candles, period=14)`**：对每根K线（从第 `period` 根开始）用它之前
`period+1` 根K线滚动算一次 ATR，除以那根收盘价，拼成"ATR 相对价格百分比"的序列——
`models.trend_state.classify_volatility` 拿这个序列跟"当前 ATR%"比较高低，不用固定绝对
阈值（同一个 ATR% 数字对不同池子意味不一样）。

### ADX（Average Directional Index，趋势强弱指标）
[`adx.py`](adx.py)

```
+DM_t = high_t - high_{t-1}（大于 -DM 且为正才算，否则记 0）
-DM_t = low_{t-1} - low_t（大于 +DM 且为正才算，否则记 0）
TR_t  = max(high_t-low_t, |high_t-close_{t-1}|, |low_t-close_{t-1}|)   （跟 ATR 同一个 TR）
对 +DM/-DM/TR 做同一套 Wilder 平滑 → +DI/-DI → DX = 100*|+DI - -DI|/(+DI+-DI) → ADX = DX 的 Wilder 平滑
```

用平滑"平均值"而不是 Wilder 原始惯用的"平滑总和"——+DI/-DI 是两个同样做法的平滑序列的比值，
周期缩放因子会互相抵消，数值上等价，这样选是为了跟 `atr.py` 共用完全同一套递推公式。
`DEFAULT_PERIOD = 14`，同 ATR。判断当前是震荡市还是单边趋势，喂给
[`../models/trend_state.py`](../models/trend_state.py) 的状态机——弱趋势（震荡）适合窄区间
吃手续费，强趋势（单边）应该放宽区间或直接退出。

**不可用**：重采样后 5 分钟K线不足 `2*period` 根（不是 `period+1`——DX 自己先要一段预热期
才稳定，再在 DX 序列上做一次平滑才是 ADX，两层平滑叠加，门槛是 ATR 的两倍）。

---

## 3. 计划中但还没实现（阶段 1 遗留 + 阶段 2，见设计文档）

以下几项设计文档里已经定义、但这个目录下还没有对应代码文件——先记在这里，等真正实现时把
公式/阈值搬到各自模块的 docstring 里，这里的条目相应改成"见 xxx.py"。**注**：阶段 1 已实施
的实际范围是 ATR/ADX + 退出信号模型（见上面各节和 `../models/README.md`），`OrderFlowImbalance`
虽然设计文档标的也是阶段 1，但不在这次实施计划（`live-signal-system-设计方案.md` 第 11 章
分阶段落地建议）圈定的范围内，留到之后单独排期。

| 指标 | 阶段 | 用途 |
|---|---|---|
| **OrderFlowImbalance** | 1（未排期） | 从逐笔 swap 买卖方向统计资金流方向性，辅助判断"高 volume 是真实双向交易还是单边资金进出" |
| **TrackingError** | 2（仅 RWA） | `\|链上价格 − 参考价\| / 参考价`，依赖参考价数据源（见设计文档 3.4 节） |
| **GapJump** | 2（仅 RWA） | 锚定真空窗口的跳空幅度分布，样本量天然小（约 50 个周末/年），要跟 σ_price 一样做"样本不足则 unavailable"处理，不能把"还没观测到问题"报告成"验证过没问题" |

复合打分（`CompositeScore`）、IL 模型（`ExpectedIL_ref`）、推荐区间这些**模型层**指标定义在
[`../models/README.md`](../models/README.md)，不在这个文档范围内。
