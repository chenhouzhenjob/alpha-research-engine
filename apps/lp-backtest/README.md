# lp-backtest

LP 池发现指标回归系统。设计方案见 [`../../docs/lp-backtest-设计方案-v1.md`](../../docs/lp-backtest-设计方案-v1.md)、
[`../../docs/pool-discovery-metrics-v1.md`](../../docs/pool-discovery-metrics-v1.md)。

## 分层架构：这个系统在做什么、不做什么

一个完整的量化交易系统通常分五层：**数据层 → 特征层 → 模型层 → 决策层 → 执行层**。
`research`（本目录所在的工作区）只负责前三层，`products/alpha-lp` 负责后两层——这不是本次实现的
取舍，是仓库根 [`AGENTS.md`](../../AGENTS.md) 定的项目边界（"Research 不保存生产私钥、不签名或广播
真实交易，也不直接修改生产数据库"；"生产策略唯一实现位于 `packages/strategies/`……不复制生产决策逻辑"）。

| 层 | 做什么 | 本项目里的模块 | 归属 |
|---|---|---|---|
| 数据层 | 把真实世界发生的事记下来，不做判断 | [`../../packages/chains`](../../packages/chains)（RPC/eth_getLogs/eth_call/WebSocket 订阅）、[`../../packages/datasources`](../../packages/datasources)（GeckoTerminal/CoinGecko）、[`../../packages/storage`](../../packages/storage)（Postgres）、[`../../packages/protocols`](../../packages/protocols)（Factory 发现/链上读取/Swap 事件解码）、`discover.py`/`seed_whitelist.py`/`ingest.py`/`qualify.py`/`register_pool.py`（`aggregate_candles.py` 已废弃为定时数据来源，代码保留供手动交叉校验；WebSocket 订阅/K 线轮询/历史快照回填现在是 [`../live-signal`](../live-signal) 常驻进程里的后台任务，不在这里） | `research` |
| 特征层 | 对当前/历史数据做确定性变换，描述"现状"，不预测、不建议 | [`../../packages/metrics/src/alpha_metrics/features/`](../../packages/metrics/src/alpha_metrics/features)：`fee_apr.py`/`cake_apr.py`/`nominal_apr.py`/`vt_ratio.py`/`volatility.py`/`capital_volatility.py`/`age_penalty.py`/`depth_tier.py`（从本目录抽到 `packages/metrics` 共享包——`lp-backtest` 和 `live-signal` 两个 app 都要用同一套计算，`apps` 之间不互相依赖，所以抽成 `packages` 给两边共用） | `research` |
| 模型层 | 引入假设做预测，或综合特征给出打分/建议 | [`../../packages/metrics/src/alpha_metrics/models/`](../../packages/metrics/src/alpha_metrics/models)：`il_model.py`（GBM 预测 IL）、`recommended_range.py`（推荐区间）、`composite_score.py`（复合打分）；[`compute.py`](../../packages/metrics/src/alpha_metrics/compute.py)/[`assemble.py`](../../packages/metrics/src/alpha_metrics/assemble.py)/[`scoring.py`](../../packages/metrics/src/alpha_metrics/scoring.py) 是跨特征层/模型层的组装入口；`validate/`（1c/1d 模型验证）、`calibrate/`（1e 模型校准）是这一层的支撑工具；[`../live-signal`](../live-signal) 的 `/conclusion` 端点是这一层对外的实时查询入口 | `research` |
| 决策层 | 结合模型建议 + 风控规则，决定要不要真的调仓 | `packages/strategies/` | `products/alpha-lp` |
| 执行层 | 真正调用合约（撤流动性、重新建仓） | `packages/execution`、`packages/signer` | `products/alpha-lp` |

`research` 的终态是给 `alpha-lp` 的决策层当"验证过的信号供应商"——通过公开纯策略接口消费，
不是自己再长出一套决策/执行逻辑。1b 目前这套 Python 特征/模型实现是一次性引导性例外
（见下方"已知技术债"），等 alpha-lp 把同一套算法做成生产纯函数模块、暴露出可调用接口后，
这里的 Python 版本要退役，切换成跨进程调用真实实现。

## 1a 阶段范围（本次已实现）

候选池发现 + 历史数据接入，对应设计方案第 4 章执行计划的 1a：

1. **候选池发现**，两条互不冲突、可以只跑其中一条也可以都跑的路径，产出都进同一张 `pool_candidates`：
   - `discover.py`：扫描 PancakeSwap V3 Factory 的 `PoolCreated` 事件，**全量**发现，水位线记录在
     `chain_cursors` 支持增量扫描——但首次全量回填要扫完整条链历史（见下方"跑多久"），成本较高。
   - `seed_whitelist.py`：**不扫描任何历史区块**，直接对 `config.py` 里配置的白名单 token 两两配对 +
     标准费率档位，调用 `Factory.getPool` 直连查询是否已存在池子（每个组合 1 次 `eth_call`）。
     只能发现"两侧都在白名单里"的池子（如 CAKE/WBNB），发现不了"白名单 token 配未知长尾币"的池子
     （如 SHIB/WBNB）——那种场景本质上需要知道对方地址，绕不开事件扫描。适合先把 ingest 全链路跑通验证，
     或者本来就只关心白名单内部的"蓝筹"配对。
2. **准入判定**（`qualify.py`）：token 白名单 + 池龄（≥7 天）+ TVL/24h volume 门槛，
   把候选池标记为 `qualified`/`rejected`。池龄判断优先用链上区块时间戳（`discover.py` 发现的候选池，
   权威、精确）；`seed_whitelist.py` 发现的候选池没有区块号，退化用 GeckoTerminal 报告的
   `pool_created_at` 兜底（不是链上权威值，但够用）。
3. **历史快照接入**（`snapshot.py`）：对 `qualified` 池子拉取近 30 天日线收盘价 + 当前 TVL/24h volume，
   写入 `pool_metrics_history`。

## 1b 阶段范围（本次已实现）：指标计算层

对应设计方案第 4 章执行计划的 1b，把 pool-discovery-metrics-v1.md 1.1-1.3 节公式在历史序列上跑通
（`features/`）。只做单项指标，不做归一化/复合打分——那是 1e"权重与分档校准"的范围。

| 指标 | 模块 | 数据依赖 |
|---|---|---|
| FeeAPR | `features/fee_apr.py` | `pool_metrics_history`（volume/TVL）+ 链上 `slot0()`（feeProtocol，当前值） |
| CakeAPR | `features/cake_apr.py` | 链上 MasterChef V3（cakePerSecond/lmLiquidityShare，当前值）+ CoinGecko（CAKE/USD，当前值） |
| NominalAPR | `features/nominal_apr.py` | FeeAPR + CakeAPR，三态正确合并（`no_incentive` 当 0，`unavailable` 不当 0） |
| VTRatio | `features/vt_ratio.py` | `pool_metrics_history`（volume/TVL） |
| σ_price | `features/volatility.py` | `pool_metrics_history`（收盘价，回看 30 天） |
| ExpectedIL_ref | `features/il_model.py` | 复用 σ_price |
| CapitalVolatility | `features/capital_volatility.py` | `pool_metrics_history`（TVL，回看 14 天） |
| AgePenalty | `features/age_penalty.py` | 池子创建时间 |
| DepthTier | `features/depth_tier.py` | 当前 TVL |

演示/验收用 CLI（对任意候选池、任意历史日期输出完整一组指标值）：

```bash
uv run --package lp-backtest lp-backtest-metrics --pool-address 0x... --date 2026-08-01
```

### CAKE emission 历史链路调研结论（1b 验收标准要求）

**不可行，降级为"当前值静态外推"**。调研了 alpha-lp 生产代码（`packages/pancake-v3/src/range-estimate.ts`
`packages/pancake-v3/src/farming.ts`）和 MasterChef V3 的 ABI，两处都只读**当前**链上状态
（`getLatestPeriodInfo`/`poolInfo`/`lmLiquidity` 都是 `eth_call` 在 `'latest'` 区块），
代码库里没有任何基于治理事件重建历史 emission rate 的逻辑，也没找到相关事件定义。
所以 `cake_apr.py` 算出来的 CakeAPR，`cakePerSecond`/`lmLiquidityShare`/`CAKE_USD 价格`
三项永远是"调用时刻的当前值"，不是 `as_of` 那一天的历史值——即便 `--date` 传的是历史日期，
这三项也不会变。这是设计方案本身已经预见并接受的降级方案（"若不可行则降级为当前值静态外推
并显式标注局限"），不是本次实现遗漏。

### 折中：CakeAPR 的 `lmLiquidityShare` 定义（需要显式说明的偏离）

alpha-lp 生产代码对应的 `lmLiquidityShare` 定义是"单个仓位份额"（`userLiquidity/(lmLiquidity+userLiquidity)`），
池子发现场景没有具体仓位，不能直接套用。本实现选择 `lmLiquidityShare = lmLiquidity / 池子当前活跃流动性`
（见 `PancakeswapV3Plugin.read_cake_emission`），语义是"这个池子当前活跃流动性里，有多大比例已经质押进
MasterChef 吃 CAKE"。

- **为什么不是 `lmLiquidityShare=1`**（即"整池都当作能吃满 CAKE 排放"）：并非所有添加进池子的流动性都会
  质押进 MasterChef 领 CAKE——不质押的那部分拿不到 CAKE，直接按 1 算会系统性高估 CakeAPR。
- **最佳终态**：如果要做到完全精确，应该同时追踪"新开仓预览"（用户即将投入的流动性稀释后的份额）
  和"已有仓位实际份额"两种口径，分别对应 alpha-lp `estimateCakeDaily`（新开仓）和
  `estimateCakeFarmDailyUsd`（已有仓位，`packages/pancake-v3/src/farming.ts`）两套现有实现；
  池子发现场景没有具体用户仓位，本实现选的是介于两者之间、面向"这个池子整体质押率"的近似值。
- **权衡**：正确性上比"份额=1"更接近真实（不会系统性高估），比"逐仓位精算"更简单（少一次仓位级 RPC 调用）；
  复杂度上只多了一次 `PancakeV3Pool.liquidity()` 调用；风险是这个定义是本次实现的解读，
  不是 alpha-lp 生产代码的直接复用，1e 阶段权重校准时如果 CakeAPR 排序明显反直觉，
  先怀疑这个口径。

## 1c 阶段范围（本次已实现）：IL 模型验证

对应设计方案第 4 章执行计划的 1c，验证 σ_price 驱动的 `ExpectedIL_ref` 预测是否可信
（`validate/il_model.py`）。方法：对每个候选池拉取约 180 天日线收盘价（GeckoTerminal 免费层实测上限），
在历史序列上按 7 天跳步滚动取样——某天 t 用"t 及之前 30 天"算出 σ_price、代入公式得到预测 IL，
再用 t 到 t+7 天的真实价格比算出实际发生的 IL，两者相减得到误差。

### 获取 / 重新生成报告

报告是一个跑一次就落盘的 Markdown 文件，不是实时查询接口——直接打开
[`validate/il_model_report.md`](validate/il_model_report.md) 看最近一次的结果，
或者重新跑一遍覆盖它：

```bash
cd research
uv run lp-backtest-validate-il-model
# 默认覆盖写到 apps/lp-backtest/validate/il_model_report.md
```

常用可选参数：

- `--limit N`：只验证前 N 个 qualified 候选池（默认全部），调试或想跑快点时用。
- `--history-days N`：每个池子拉多少天历史（默认 180，GeckoTerminal 免费层实测上限附近）。
- `--stride-days N`：滚动取样跳步（默认 7，和 `T_ref` 一致，避免相邻窗口重叠太多把同一段价格路径重复计入误差）。
- `--output PATH`：报告写到别的路径，不覆盖默认那份。

最近一次真实运行（27 个 qualified 候选池，567 个样本）：平均绝对误差 0.028%、RMSE 0.043%。

**范围缩减（需要显式说明）**：设计方案原文的目标是"验证是否比原四档币对分类更准"，
本次实现只做了"σ_price 模型自身的预测误差分布"，没有做"和原四档分类的对比"——
调研 alpha-lp 生产代码没有定位到"原四档分类"具体的假设 IL 数值（不像 FeeAPR/CakeAPR 那样有明确公式可查），
为避免拿一个瞎猜的数字冒充对比，这部分对比留作后续单独调研，报告里也如实写明了这一点。

## 1d 阶段范围（本次已实现）：推荐区间回测

对应设计方案第 4 章执行计划的 1d，回测 pool-discovery-metrics-v1.md 1.4 节的三档推荐区间
（主动型 3 天/平衡型 7 天/被动型 30 天）在真实历史价格路径下的表现
（`validate/range_backtest.py`，复用 `features/recommended_range.py` 的区间宽度公式）。

```bash
uv run --package lp-backtest lp-backtest-validate-range-backtest
# 报告写入 apps/lp-backtest/validate/range_backtest_report.md
```

三项输出，对应验收标准要求的"实际出界时间、手续费捕获、IL，跟模型预测对比"：

- **出界情况**：目标周期内实际出界的比例、平均资金利用率（留在区间内的时间占比）。
- **手续费捕获**：只回测"资金效率利用率"（真实价格路径决定，不依赖历史 volume）×
  集中流动性资金效率倍数（Uniswap V3 白皮书近似公式 `1/(1-e⁻ʷ)`），**不是**"历史每天赚了多少美元"——
  后者需要历史 volume，GeckoTerminal 免费层拿不到（同 1a/1c 已记录的限制），
  用"今天的费率套用到历史每一天"冒充历史回测会把 volume 随价格波动的真实变化抹平，故意没这么做。
- **IL 对比**：复用 1c 同一套 `predicted vs realized` 方法，只是把 T_ref 从固定 7 天换成三档各自的 T_target。

最近一次真实运行（27 个 qualified 候选池，1782 个样本）：三档出界比例 28%/34%/38%，
资金利用率 91%/87%/82%，IL 预测 MAE 0.015%/0.028%/0.174%（随周期变长而变大，符合预期）。

## 1e 阶段范围（本次已实现）：权重与分档校准

对应设计方案第 4 章执行计划的 1e，拿全部 qualified 候选池的当前指标跑一遍
`features/composite_score.py` 的默认权重（0.30/0.25/0.25/0.10/0.10），
输出分项相关性、分数分布、S/A/B/C 分档数量，以及基于真实数据的校准建议
（`calibrate/weights.py`，只产出建议，不直接改权重——改权重是产品决策，不是这次分析该自己定的）。

```bash
uv run --package lp-backtest lp-backtest-calibrate-weights
# 报告写入 apps/lp-backtest/calibrate/weights_report.md
```

**最近一次真实运行的核心发现（比表面上的"跑通了"更重要）**：27 个候选池里 0 个 S、1 个 A、
0 个 B、26 个 C——分档几乎完全塌缩到 C 档，因为原公式的理论可达分数上限（约 0.55）
本来就够不到 S 档阈值（0.75）。这不是候选池质量差，是复合分公式的加分项/扣分项权重结构和
S/A/B/C 阈值本来就没有对齐，属于设计方案自己承认的"首版经验值，需要用真实数据校准"（5. 已知局限）
预见到的情况。详细发现（含 NominalAPR/VTRatio 高相关、CapitalVolatility 零覆盖率、
AgePenalty 名义权重和实际影响力不成比例）见报告。

## 1f 阶段范围（本次已实现）：产物输出

对应设计方案第 4 章执行计划的 1f，产出 alpha-lp 可消费的版本化权重配置 + 汇总 1c/1d/1e 结论的报告。

```bash
uv run --package lp-backtest lp-backtest-export-config
# 输出 apps/lp-backtest/config/composite_score_weights.v1.json
```

- 配置文件的权重/阈值直接从 `features/composite_score.py` 的真实常量导出（`config_export.py`），
  不是手抄一份数字，避免配置和代码实际使用的值不同步。
- 汇总报告见 [`SUMMARY_REPORT.md`](SUMMARY_REPORT.md)，列了 1c/1d/1e 的核心结论和
  "需要 alpha-lp 团队人工决策的事项"——设计方案本身要求"alpha-lp 团队确认这份配置格式可以直接接入"，
  这是需要人工完成的验收步骤，本次实现只能产出格式本身、把决策点列清楚，没法替团队做这个确认。

## 使用
```bash
# 装依赖(如果还没装过)
uv sync --all-packages

# 方式一：全量扫描发现（能发现任意长尾池子，但首次全量回填耗时较长，见下方"跑多久"）。
# 首次运行会按协议上线时间锚点二分定位起始区块；之后按 chain_cursors 水位线增量扫描。
uv run --package lp-backtest lp-backtest-discover

# 方式一变体：只想要最近一段时间的池子，不想扫完整条链历史，用 --since-days-ago
# 二分定位"大约 N 天前"对应的区块作为起点（忽略已有水位线和协议上线锚点，放弃更早的候选池）。
uv run --package lp-backtest lp-backtest-discover --since-days-ago 365

# 方式二：白名单直连发现（几十次 eth_call 就能跑完，几秒钟出结果，但只能发现白名单内部配对）。
uv run --package lp-backtest lp-backtest-seed-whitelist

# 两种方式可以都跑，产出的候选池共存于同一张表，不冲突、不重复。

# 对新发现的候选池跑准入判定 + 拉取近 30 天历史快照。
uv run --package lp-backtest lp-backtest-ingest

# 调试：只处理少量池子，跳过重新判定准入。
uv run --package lp-backtest lp-backtest-ingest --skip-qualify --limit 5
```

### `discover.py` 全量回填要跑多久

实测（截至本次实现时）：PancakeSwap V3 BSC 上线区块到链上最新区块约 8900 万个区块。`eth_getLogs`
按 `BNB_LOG_CHUNK_SIZE`（默认 45000，见 [`alpha_chains/bsc.py`](../../packages/chains/src/alpha_chains/bsc.py)）
分段，全量回填约需 **2000 次 RPC 调用**（一次性成本，之后只增量扫描新区块）。
如果暂时不想跑这个,先用 `lp-backtest-seed-whitelist` 把 ingest 全链路跑起来即可。

**`--since-days-ago` 能省多少不一定符合直觉**：实测"1 年前"对应的区块号距当前链高约 5800 万个区块，
而全量是约 8900 万——BSC 出块速度中途提速过（Fermi 升级后 0.45s/块），越靠近现在单位时间的区块数越多，
所以"只扫最近 1 年"省下来的调用次数比表面上"1 年 vs 3.3 年"看起来的要少,大约能省三分之一,不是省大头。

**注意**:`--since-days-ago`（以及 `--from-block`）会把 `chain_cursors` 水位线直接推进到这次运行的终点,
如果之前没跑过全量扫描,之后再想补回更早的候选池,需要显式传 `--from-block` 指定更早的区块,
不会自动"回头补扫"。

## 明确排除（1a 不做，见设计方案 3.2）

- 第 3 节退出信号（依赖 alpha-lp 生产 `positions` 数据）。
- 第 4 节 RWA 扩展（参考价数据源未选型）。
- 大户集中度（v1 暂缺数据源）。
- 跳仓频率 `w_typical`（依赖 TickLens 历史快照，需要 archive node）。

## 已知技术债（不要遗忘）

- **Python 参考实现，非终态**：本期指标计算/数据接入是 Python 一次性引导实现。等 alpha-lp 把这套指标计算
  做成生产的纯函数模块之后，research 的验证/回测要切换成跨进程调用 alpha-lp 的真实实现，这里的 Python 版本退役
  （见 [`../../docs/lp-backtest-设计方案-v1.md`](../../docs/lp-backtest-设计方案-v1.md) 1.2/4.2 节）。
- **GeckoTerminal 不提供 TVL/24h volume 的历史时间序列**：免费公开 API 的池子端点（含批量 `/pools/multi`）
  只暴露"当前值"。`pool_metrics_history.tvl_usd`/`volume_24h_usd` 历史行会是 `NULL`，
  这两个指标只能从系统首次采集当天开始逐日运行 `ingest` 积累，无法一次性回填过去 30 天。
  只有 `close_price`（来自 OHLCV 日线端点）可以一次性回填约 30 天历史，`σ_price` 等波动率指标（1c 阶段）
  不受此限制影响；但 `CapitalVolatility`（近 14 日 TVL 环比波动，见 pool-discovery-metrics-v1.md 1.3 节）
  在积累出足够天数的真实历史之前，会持续标记 `unavailable`。
- **CAKE emission 历史链路调研结论**：不可行，降级为"当前值静态外推"，详见上面 1b 章节，
  已按设计方案要求写入文档，不是遗漏。
- **TVL/24h volume 门槛数值是经验默认值**：`config.py` 里的 `MIN_TVL_USD`/`MIN_VOLUME_24H_USD`
  是本期占位默认值，不是最终结论，需要 1e 阶段用真实候选池数据校验是否合理（池龄门槛 7 天例外——
  这个数值原文档已明确给出，不是占位）。
- **`/pools/multi` 单次最大地址数（`MAX_ADDRESSES_PER_MULTI_CALL=30`）是保守假设**：GeckoTerminal 官方文档
  未给出该端点的地址数上限，本期按限流数量级取的保守值，未观察到"参数过多"报错；若未来报错需要下调。
- **`seed_whitelist.py` 发现的候选池 `created_at` 不是链上权威值**：`Factory.getPool` 只返回地址，
  没有部署区块号，只能退化用 GeckoTerminal 报告的 `pool_created_at` 兜底池龄判断（见 `qualify.py`
  的 `_resolve_created_at`）。如果需要链上精确创建时间，用 `discover.py` 的事件扫描路径。
- **GeckoTerminal 限流比官方文档更严**：官方文档写 30 次/分钟，实测按 30 卡节流仍会偶发 429，
  已下调到 20 次/分钟并把重试放宽到 8 次/最长 90 秒退避（见 `geckoterminal.py`）。如果还是偶发 429，
  再往下调 `RATE_LIMIT_CALLS_PER_MINUTE`。
- **（已修复，记录在案）`get_daily_ohlcv` 曾用 `currency=usd` 拉历史收盘价，和 `get_pool_snapshots`
  的 `base_token_price_quote_token`（token0/token1 汇率）单位对不上**：同一个 `close_price` 列历史行是
  "CAKE 的 USD 价格"、"今天"这一行却是"CAKE/WBNB 汇率"，两者能差几百倍，1b 阶段算 σ_price 时
  产出过一次离谱到 2270% 年化的波动率才发现。现已统一改成 `currency=token`（两个数据源都是
  token0/token1 汇率），并重跑过一次 `ingest` 修复了已入库的历史数据。**如果你的本地数据库是在这次修复
  之前跑的 `ingest`，需要重新跑一遍 `lp-backtest-ingest --skip-qualify` 覆盖旧的 `close_price`。**
