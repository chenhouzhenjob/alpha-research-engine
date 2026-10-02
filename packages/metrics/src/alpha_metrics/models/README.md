# models/ 指标说明文档真相

这个目录下每个模型的**唯一权威定义**在这个文件里——公式、阈值、三态判定条件的改动必须在
同一次改动里同步这个文档，跟 [`../features/README.md`](../features/README.md) 之于特征层、
[`SCHEMA.md`](../../../../../SCHEMA.md) 之于表结构是同一个约定，见文末"维护约定"一节。

跟 `features/` 的边界：这里的模块会引入模型假设（如 GBM 无漂移）或做多指标综合打分，产出的是
"预测值"/"建议"，不是对现状的单纯数学变换——纯特征（`FeeAPR`/`VTRatio`/`σ_price` 等）的定义在
[`../features/README.md`](../features/README.md)，不重复列在这里。

---

## 1. IL（无常损失）模型

[`il_model.py`](il_model.py)。两个函数共用同一个核心公式，保证"预测 IL"和"实际发生的 IL"
可以直接比较（1c 阶段验证靠的就是这个）。

### realized_il_from_price_ratio(price_ratio)

```
IL% = 2√(priceRatio) / (1 + priceRatio) − 1
```

标准 IL 公式。对称——`price_ratio` 和它的倒数算出来结果一样，调用方不用操心"是终值/初值
还是初值/终值"这个方向问题。返回负数（如 `-0.006` = 亏 0.6%）。

**报错**：`price_ratio <= 0` 时抛 `ValueError`。

### expected_il_ref(sigma, t_ref_days=7)

```
k = exp(σ × √(t_ref_days / 365))
ExpectedIL_ref = realized_il_from_price_ratio(k)
```

`DEFAULT_T_REF_DAYS = 7`。用"一个标准差的价格变动"代入标准 IL 公式来近似期望 IL——**不是**
在整个概率分布上积分算出的严格期望值，是一阶近似。`t_ref_days=7` 是打分口径的固定约定
（不随用户实际选的区间宽度变），保证不同池子的复合分之间可比。

### expected_il_ref_from_closes(closes, t_ref_days=7)

先用 `features.volatility.sigma_price` 算 σ_price，再喂给 `expected_il_ref`；σ_price
`UNAVAILABLE` 时原样透传这个不可用状态（带上它的 reason，或者一个默认的"σ_price 不可得"）。

---

## 2. 推荐区间模型

[`recommended_range.py`](recommended_range.py)

### recommended_width(sigma, t_target_days)

```
w_recommended(T_target) = σ_price × √(T_target / 365)
```

返回对数价格空间里的对称半宽（`ln(P_upper/P_current)`）。然后
`P_upper = P_current × exp(+w)`，`P_lower = P_current × exp(−w)`。

`MAINTENANCE_PROFILES = {"主动型": 3, "平衡型": 7, "被动型": 30}`（天）——平衡型的 7 天
正好对应 IL 打分口径的 `DEFAULT_T_REF_DAYS`，不是巧合。

### capital_efficiency(width)

```
capital_efficiency = 1 / (1 − exp(−width))
```

Uniswap V3 白皮书推导的集中流动性相对全范围做市的资金效率倍数——只有价格正好在区间几何
中心时才精确成立，价格偏向区间一侧时会有偏差，当一个数量级参考用，不是精确倍数。

**边界处理**：`width <= 1e-9`（σ_price 接近 0，比如锚定很死的稳定币对）时理论上效率趋于
无穷大，函数截断到 `1e6` 避免除零——调用方展示这类极端值时应该说"资金效率极高"，不是照抄
这个具体数字当真实倍数。

---

## 3. 归一化 + 复合打分

### normalize_min_max
[`normalize.py`](normalize.py)

在**当前候选集**内把一组值 min-max 归一化到 `[0,1]`（不是固定的绝对阈值）——"什么算高 APR"
会随大盘行情漂移，相对排名比绝对切点更稳定。

**边界处理**：全部相同（或只有一个值）时统一返回 `0.5`——不能除零，也不能武断给 0 或 1
（那等于说"和别人比是最低/最高"，但根本没有可比的差异）。

**候选集太小时会失真**：只有 2 个候选池时，任意一项指标的归一化结果只会是 0.0 和 1.0
这两个值（谁大谁小直接决定），跟"这个数值在市场里算好还是差"没有关系——这不是 bug，
是这个函数在小候选集下的必然行为，候选集扩大之后才会有真实的排名意义。

### CompositeScore
[`composite_score.py`](composite_score.py)

```
CompositeScore = normalize(NominalAPR)×0.30 + normalize(VTRatio)×0.25
                − normalize(|ExpectedIL_ref|)×0.25 − normalize(CapitalVolatility)×0.10
                − AgePenalty×0.10
```

`NORMALIZED_COMPONENT_WEIGHTS = {"nominal_apr": (0.30,+1), "vt_ratio": (0.25,+1),
"il_risk": (0.25,-1), "capital_volatility": (0.10,-1)}`；`AGE_PENALTY_WEIGHT = 0.10`
（直接用原始值 0/0.05 相乘，不参与归一化）。

**符号澄清**（原文档没写清楚，这里是必要的解读）：`ExpectedIL_ref` 恒为负数。如果直接对
带符号的负数做归一化，"损失最大"会被映射到 0、"损失最小（最接近 0）"被映射到 1，再乘负号
扣分——正好和公式本意相反（风险最低的池子被扣得最多）。修法是对 `|ExpectedIL_ref|`（幅度）
做归一化，让"风险越大 → 归一化值越大 → 扣分越多"，这个归一化后的分项叫 `il_risk`。

**AgePenalty 不参与归一化**：是唯一一个直接用原始值（不做 min-max）乘权重的分项，也不参与
"数据缺失按比例分摊权重"的重新归一逻辑（那个逻辑只发生在另外四项之间）。`age_penalty_value`
是 `None`（池子创建时间未知）时直接跳过这一项扣分，不把它的 0.10 权重分给其他四项。

**缺数据处理**（`compute_composite_score`）：4 个可归一化分项里任意一项是 `None`，就丢弃，
剩余分项按权重比例重新分摊：`score += sign × value × (weight / total_weight_available)`。

**分档阈值**（`TIER_THRESHOLDS`）：S≥0.75，A≥0.55，B≥0.35，其余 C。这是设计文档给的原始
绝对阈值，**没有**针对这份实现的实际可达分数范围重新校准过——1e 校准报告已经指出可能对不上。

`CompositeScoreResult.confidence = 可用分项数 / 5`（4 个可归一化分项 + AgePenalty，
AgePenalty 可用记 1，不可用记 0）。

`scoring.py` 负责在一批候选池上编排这整套流程：`collect_raw_metrics` → 逐分项
`normalize_all_components` → `compute_composite_scores`。这套编排本身就是"复合分是候选集内
相对排名，不能只对一个池子单独算"这件事的直接体现，`live-signal` 的 `/conclusion` 端点和
`lp-backtest-calibrate-weights` 共用同一份逻辑，不是各写一遍。

### compute_percentile_ranks(scores)
[`../scoring.py`](../scoring.py)（阶段 1 新增，退出信号 4"相对排名坍塌"用）

对一批 `CompositeScoreResult` 按 `score` 排序，映射到 `[0.0, 1.0]`——0.0 是候选集里分最低，
1.0 是分最高，按排序位置线性分布，不做插值。**候选池数 < 2 时全部标记 unavailable**——跟
上面 `normalize_min_max`"候选集只有 2 个时归一化会失真"是同一个道理，候选池只有 1 个时
排名根本无从谈起。

---

## 4. 趋势/波动率状态模型

[`trend_state.py`](trend_state.py)（阶段 1 新增）。基于 `features.adx`/`features.atr_pct_series`
的简单状态机，决定用上面"推荐区间模型"`MAINTENANCE_PROFILES` 里哪一档更合适——不是新公式，
是"选哪个已有推荐档位"的规则。**首版经验值，未针对真实数据校准**。

**趋势分档**（`classify_trend`，3 档，不是设计文档字面的二分"强/弱"）：

```
ADX > 25   -> "strong"（强趋势）
ADX < 20   -> "weak"（弱趋势/震荡）
其余（20-25）-> "transitional"（过渡）
```

ADX 20-25 之间是业界公认的模糊区间，不硬按单一切点二分，独立成"过渡"档，统一落到"平衡型"，
不赌方向——这跟设计文档字面的"四象限"有出入，是一次确认过的调整。

**波动分档**（`classify_volatility`）：不用固定绝对阈值，跟这个池子自己最近
`ATR_PCT_HISTORY_WINDOW`（30）根 5 分钟K线的 ATR% 中位数比——高于中位数算"高波动"（`high`），
反之"低波动"（`low`）。同一个 ATR% 数字对不同池子、不同市场环境意味不一样，跟
`normalize_min_max`"相对候选集/自身历史，不用绝对阈值"是同一个哲学。历史为空时统一归为
`low`（没有历史就不该轻举妄动）。

**6 格映射表**（`select_maintenance_profile`，3 档趋势 × 2 档波动）：

| ADX 趋势 | ATR% 波动 | 推荐档位 | 理由 |
|---|---|---|---|
| 强 | 高 | 被动型(30天) | 设计文档："强趋势应该放宽区间或直接退出" |
| 强 | 低 | 平衡型(7天) | 趋势明确但波动不大，不用最宽也不用最窄 |
| 过渡 | 高/低 | 平衡型(7天) | 趋势不明确，不赌方向 |
| 弱 | 高 | 平衡型(7天) | 震荡但波动大，窄区间容易来回被扫 |
| 弱 | 低 | 主动型(3天) | 设计文档："震荡市适合窄区间吃手续费" |

`adx`/`atr_pct` 任一 `unavailable` → 整体 `unavailable`，不能在信息不全的情况下悄悄选一个档位。

`live-signal` 的 `/conclusion` 端点用这个模型选出的档位，接上面"推荐区间模型"的
`recommended_width`/`capital_efficiency`（不重新发明区间数学），再用
[`../../protocols/src/alpha_protocols/tick_math.py`](../../protocols/src/alpha_protocols/tick_math.py)
（阶段 1 新增）换算成 `tick_lower`/`tick_upper`。

---

## 5. 退出信号模型

[`exit_signals.py`](exit_signals.py)（阶段 1 新增，对应 `pool-discovery-metrics-v1.md` §3.2
的 5 条信号）。发现框架看的是候选集内的相对排名，这里换成"相对开仓时/相对近期均值的退化
趋势"——同样是"分低"，一个新池子分低说明本来就不该进，一个持仓池分低说明"变差了"，语义不同，
不能直接套用发现框架的 S/A/B/C 分档。

5 条信号里，信号 1（NetEdge）、2（CAKE 撤出）、4（排名坍塌）是纯池子级别信号，不需要仓位
数据；信号 3（VTRatio 枯竭）、5（累计倒亏）需要"开仓那一刻"的基线值，由调用方（如 alpha-lp，
它自己有 `positions` 表）通过 `/conclusion` 的 4 个可选 query 参数传入——不传就是
`unavailable`，不阻塞其余信号。

1. **NetEdge < 0**（`net_edge_negative`/`net_edge_value`）：
   `NetEdge = NominalAPR × (7/365) + ExpectedIL_ref`（IL 恒为负）。两项都是每次请求现算的
   （`compute_daily_metrics` 每次都重新算），天然满足"用当前 σ_price 重新计算，而非开仓时的
   旧值"这个要求，不需要额外传参。`NominalAPR`/`ExpectedIL_ref` 任一不可用 → 整个信号不可用。

2. **CAKE 激励撤出**（`cake_incentive_withdrawn`）：`CakeAPR` 从 `available` 变成
   `no_incentive`。**已知局限**：没有仓位基线的话，分不清"这个池子从来没有 CAKE"和
   "CAKE 被撤了"——两种情况看起来一样，阶段 1 接受这个局限（大概率两种情况都不该继续做市，
   误判后果不严重）。`CakeAPR` 本身读不到（`unavailable`）时这条信号也标记不可用。

3. **VTRatio_MA(7日) 下降**（`vtratio_ma7_drop_pct`）：`(当前MA7 - position_open_vtratio_ma7) /
   position_open_vtratio_ma7`。用 `features.vt_ratio.vt_ratio_ma` 从 `pool_metrics_history`
   现有的 `history` 逐日算，不需要新查询。`position_open_vtratio_ma7` 未传 → 不可用。
   默认判定阈值 `VTRATIO_MA7_DROP_THRESHOLD = -0.5`（设计文档"如 -50%"，带"如"字，不是精确
   要求）。

4. **相对排名坍塌**（`composite_rank_percentile`）：由调用方（`/conclusion` 端点）用上面的
   `compute_percentile_ranks` 对全量候选池算好传入——`models/` 不反过来依赖 `scoring.py`
   （`scoring` 在 `models` 之上编排，不是反过来）。默认判定阈值
   `COMPOSITE_RANK_PERCENTILE_THRESHOLD = 0.5`（设计文档"如后 50%"）。

5. **累计已实现 IL 超过累计手续费**（`cumulative_realized_il_exceeds_fees`/`realized_il_usd`）：
   `price_ratio = current_price / position_open_price`，
   `realized_il_usd = |realized_il_from_price_ratio(price_ratio)| × position_open_value_usd`，
   跟 `position_cumulative_fees_usd` 比较。`current_price` 现场查一次链上 `slot0()`
   （`PancakeswapV3Plugin.read_slot0_price_and_tick`，短缓存 15 秒，不像 `feeProtocol`/CAKE
   排放缓存 5 分钟——价格变化快得多）；任一仓位参数缺失或链上读取失败 → 不可用。这条信号
   不依赖 GBM 假设或波动率模型，只用实际发生的价格变化和实际到手的收入对比，设计文档原文
   建议在几条信号里权重最高。

`any_signal_fired(signals)`：5 条信号里任意一条"可用且触发" → `True`，供 `/conclusion` 的
`recommended_action` 判断（任一触发 → `exit`，否则退回 `CompositeScoreResult.tier` 的
S/A/B/C 映射）。**不在这里判断 `rebalance`**——`models/`/`live-signal` 都不知道调用方当前
仓位的实际 tick 范围，这个比较只有调用方自己能做。

---

## 6. 维护约定

**新增/修改 `features/` 或 `models/` 下的任何指标（公式、阈值、三态判定条件、权重）时，
必须在同一次改动里同步对应的 README**（这里或 [`../features/README.md`](../features/README.md)）——
不能只改代码不改文档，两者跑偏之后这份文档就失去了"唯一权威定义"的意义。

判断改哪份文档很简单：看代码文件在哪个目录下——`features/*.py` 改动同步 `features/README.md`，
`models/*.py` 改动同步这份 `models/README.md`。
