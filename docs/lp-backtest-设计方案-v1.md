# LP 池发现指标回归系统（lp-backtest）· 设计方案 v1

## 0. 文档定位

本方案回答两个问题：

1. [`pool-discovery-metrics-v1.md`](pool-discovery-metrics-v1.md) 里定义的池子发现指标体系，哪些部分该落在 `products/alpha-lp`，哪些该落在 `research`。
2. 落在 research 这部分（权重校准、模型验证）具体怎么拆解、一期做到什么程度。

本文档达成一致后，直接按第 4 章的执行计划实施，不再另开设计讨论。

## 1. 架构边界：products 与 research 怎么分工

### 1.1 判断标准

不按"在线/离线"分，按"**管理真实资金/执行 vs 分析历史数据/出建议**"分：

| | `products/alpha-lp`、`products/alpha-grid` | `research/` |
|---|---|---|
| 语言 | TypeScript | Python |
| 是否管理真实资金 | 是 | 否，只读不写链，不碰私钥 |
| 是否可以常驻在线跑 | 是 | **也可以**——research 允许有常驻的摄取/调度/告警/API 服务，"在线"不等于"是 product" |
| 产出 | 真实下单、真实调仓 | 报告、校准后的参数配置（版本化产物） |
| 依赖方向 | 禁止反向依赖 research，也不得直接依赖彼此的业务实现 | 只能通过产品暴露的"公开纯策略接口"调用，不能调用产品的执行接口 |

依据：`products/alpha-lp/AGENTS.md` 已有"生产策略唯一实现位于 `packages/strategies/`；未来 Research 回测通过公开纯策略接口消费，不复制生产决策逻辑"；`products/alpha-grid/README.md` 已有"向 Research 暴露无 I/O 的纯策略接口"、"不建设独立的历史行情数据湖 / 不维护另一套回测平台"。本方案沿用这两条已有约定，不是新发明。

### 1.2 跨语言调用两种模式，不要混用

- **数据解析类逻辑**（比如 GeckoTerminal OHLCV 怎么归一化）：TS 和 Python 各自实现一遍，**共享的是契约 + 测试样本**（放 `shared/` 或 `tests/contract/`），不共享可执行代码。这类逻辑重写一遍风险可控。
- **策略/打分决策逻辑本身**（比如这次要回测的复合打分公式）：**不能重写**，必须跨进程调用同一份代码——因为回测的意义就是验证"生产真实会跑的算法"，Python 重写一份"差不多"的版本会让回测结果失去意义。落地方式是给 `packages/strategies` 一类无 I/O 纯函数包一层薄的子进程/本地 socket 调用（读 JSON 状态、吐 JSON 决策），Python 侧的模拟循环反复调用。

**本期现实情况**：pool-discovery-metrics-v1.md 的指标计算目前在 alpha-lp 里还没有生产实现，所以一期只能先在 research 里用 Python 写一份公式的参考实现——这是**一次性引导性例外**，允许存在，但要在文档里显式挂账：等 alpha-lp 把这套指标计算做成生产的纯函数模块之后，research 的验证/回测要切换成调用 alpha-lp 的真实实现，Python 版本退役，避免两边长期各自演化导致对不上账。

## 2. research 内部结构

不是每个研究方向各建一个独立仓，而是一个 Python workspace，共享数据接入基建，域相关的放各自的 `apps/`：

```
research/
├── packages/                # 跨域共享，唯一碰外部数据源和数据库的地方
│   ├── core/                 # 领域模型（Chain/Token/PoolSnapshot 等），无 I/O
│   ├── storage/               # 唯一直接碰 DB 的地方
│   ├── chains/                 # 链适配器（get_logs/get_block 统一接口）
│   ├── datasources/            # 外部数据源，按优先级降级（Subgraph → 推送 → RPC）
│   └── protocols/               # 协议插件（Factory 自动发现、字节码指纹等）
└── apps/
    ├── wallet-analyzer/        # 已有设计：钱包链上行为分析，只读分析
    ├── lp-backtest/            # 本方案新增，见第 3 章
    └── grid-backtest/          # 占位，Grid 迁入后再展开，同一套模式
```

`packages/{chains,datasources,protocols}` 的雏形来自 [`钱包链上行为分析系统设计方案.md`](钱包链上行为分析系统设计方案.md) 第 3 章的"共享全链摄取"设计，本方案直接复用，不重新实现一遍 Factory 发现、数据源降级这些机制。

命名约定：只读分析类 app 用 `-analyzer` 后缀，回测/校准类用 `-backtest` 后缀。

## 3. lp-backtest 范围

### 3.1 一期要回答的三个问题

对应 pool-discovery-metrics-v1.md 第 5 章"已知局限"里明确写"需要校准/验证"的部分：

1. σ_price 驱动的 `ExpectedIL_ref` 近似，历史上准不准？（估计 IL vs 价格真实走出来之后算的实际 IL 对比）
2. 3/7/30 天三档推荐区间历史回测：实际出界时间、实际手续费捕获、实际 IL，跟模型预测差多少？
3. 复合分权重（0.30/0.25/0.25/0.10/0.10）拿真实候选池数据跑一遍分布是否合理——分项之间是否高度相关导致重复计分、AgePenalty 固定扣分和归一化分项尺度是否匹配、S/A/B/C 分档阈值切出来的池子分布是否符合直觉。

### 3.2 明确排除（一期不做）

- **第 3 节退出信号**：依赖 alpha-lp 生产库里真实的 `positions` 数据，等 alpha-lp 上线发现功能、积累真实持仓后再做。
- **第 4 节 RWA 扩展**：参考价数据源未选型。
- **大户集中度**：原文档已标"v1 暂缺数据源"。
- **跳仓频率 `w_typical`（依赖 TickLens 历史快照）**：普通 RPC 没有历史 state，需要 archive node，成本高。一期用**固定候选区间宽度 + 价格路径模拟首次出界时间**代替，不复现历史 TickLens 分布。

### 3.3 数据接入

| 数据 | 来源 | 说明 |
|---|---|---|
| 候选池全量地址 | PancakeSwap V3 Factory `PoolCreated` | 复用钱包分析方案"工厂自动发现"机制，参数从"钱包涉及的池子"换成"Factory 全量池子" |
| 历史 TVL/24h volume/OHLCV | GeckoTerminal | `datasources/` 新增适配器 |
| CAKE emission 历史 | MasterChef V3 治理事件 | **待确认项**——现在只能实时读 `poolInfo`，历史值需要从治理事件重建，一期先调研可行性，不可行则降级为"当前值静态外推"并显式标注局限 |
| Token 白名单/门槛 | 复用 pool-discovery-metrics-v1.md 第 0 节规则 | 直接照抄，不改设计 |

统一落 `pool_metrics_history` 表，按天存快照。

## 4. V1 执行计划

| 阶段 | 目标 | 交付物 | 验收标准 |
|---|---|---|---|
| **1a** 候选池发现 + 历史数据接入 | 打通 Factory 全量发现 + GeckoTerminal 历史抓取 | `packages/datasources` 新增 GeckoTerminal 适配器；`packages/protocols` 扩展全量池子发现用途；`pool_metrics_history` 表落地 | 能拉出 BSC 全量 PancakeSwap V3 候选池近一个月的日线快照 |
| **1b** 指标计算层 | 把 1.1-1.3 节公式在历史序列上跑通 | `apps/lp-backtest/features/`：FeeAPR / CakeAPR / VTRatio / σ_price / `ExpectedIL_ref` / CapitalVolatility / AgePenalty 的每日计算 | 对任意候选池、任意历史日期，能输出完整的一组指标值；CAKE emission 历史链路调研结论已写入文档（可行 or 降级方案） |
| **1c** IL 模型验证 | 验证 σ_price 驱动的 IL 估计是否比原四档分类更准 | `apps/lp-backtest/validate/il-model.py` + 验证报告 | 输出"预测 IL vs 实际 IL"的误差分布，覆盖足够数量的历史时间点/池子 |
| **1d** 推荐区间回测 | 验证 3/7/30 天推荐区间的历史表现 | `apps/lp-backtest/validate/range-backtest.py` + 报告 | 输出三档区间在历史价格路径下的实际出界时间、手续费捕获、IL，跟模型预测对比 |
| **1e** 权重与分档校准 | 校准复合分权重、检验分档阈值 | `apps/lp-backtest/calibrate/weights.py` + 校准报告 | 给出校准后权重建议 + 分项相关性/分布分析 + S/A/B/C 分档池子数量分布 |
| **1f** 产物输出 | 定义并产出 alpha-lp 可消费的配置格式 | 版本化 JSON 权重配置 + 汇总报告（含 1c/1d/1e 结论） | alpha-lp 团队确认这份配置格式可以直接接入未来的生产实现 |

### 4.1 阶段间关系

1a → 1b 是严格顺序（没数据就没指标）；1c、1d、1e 都依赖 1b 的产出但彼此独立，可以并行推进；1f 汇总前三者结论，是最后一步。

### 4.2 已知技术债（写入 `apps/lp-backtest/README.md`，不要遗忘）

- 一期指标计算是 Python 参考实现，等 alpha-lp 生产落地同一套公式后，必须切换成跨进程调用 alpha-lp 的真实实现，Python 版本退役。
- CAKE emission 历史重建的可行性在 1b 阶段才会有结论，若不可行，1c/1d 里涉及 CakeAPR 的部分需要标注"基于当前 emission 静态外推"的局限，不能包装成历史精确值。
