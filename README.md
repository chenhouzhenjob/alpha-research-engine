# research

Alpha 系列的统一数据采集、特征、仿真与回测平台。只读不写链，不持有生产签名权限
（详见仓库根 [`AGENTS.md`](../AGENTS.md) 的项目边界）。

**分层边界**：一个完整系统分五层——数据层/特征层/模型层/决策层/执行层。`research` 只做前三层
（采集数据、算特征、跑模型给出预测/打分），`products/alpha-lp` 做后两层（结合风控规则决策、
真正调用合约执行）。`research` 不碰私钥、不签名、不广播交易，也不复制 alpha-lp 的生产决策逻辑。
各层具体对应哪些模块，见 [`apps/lp-backtest/README.md`](apps/lp-backtest/README.md#分层架构这个系统在做什么不做什么)
的详细表格（其他 `apps/` 沿用同一套分层约定）。

采用单一 Python workspace（uv workspace），不是每个研究方向各建一个独立仓：`packages/` 放跨域共享的基建，
域相关的代码放各自的 `apps/`。详见 [`docs/lp-backtest-设计方案-v1.md`](docs/lp-backtest-设计方案-v1.md) 第 2 章。

```
research/
├── packages/              # 数据层基建，跨 apps 共享
│   ├── core/               # 领域模型，无 I/O
│   ├── storage/             # 唯一直接碰 DB 的地方（ORM + Alembic 迁移 + 仓储）
│   ├── chains/               # 链适配器（get_logs/get_block 统一接口）
│   ├── datasources/           # 外部数据源（GeckoTerminal/CoinGecko 等）
│   └── protocols/              # 协议插件（Factory 自动发现、MasterChef 读取等）
└── apps/
    └── lp-backtest/          # LP 池发现指标回归系统，见该目录 README
        └── src/lp_backtest/
            ├── features/       # 特征层：确定性变换，不预测
            ├── models/          # 模型层：引入假设做预测/打分
            ├── compute.py        # 组装：拼出一个池子/日期的完整信号包
            ├── validate/          # 模型验证（1c/1d）
            └── calibrate/          # 模型校准（1e）
```

## 本地开发

```bash
# 先起 LP 的 Postgres/Redis 实例（本目录不另起库进程）
docker compose -f ../products/alpha-lp/infra/docker-compose.yml up -d
cd research
docker compose up              # 在同一实例上创建 alpha_research 库（已存在则跳过）
cp .env.example .env           # 填 BNB_RPC_URLS；RESEARCH_DATABASE_URL 默认指向 127.0.0.1:5432/alpha_research
uv sync                        # 装全部 workspace 成员
(cd packages/storage && uv run alembic upgrade head)  # alembic.ini 的 script_location 是相对路径，需要在该目录下执行
```

表结构唯一文档真相：[`SCHEMA.md`](SCHEMA.md)。改表必须在同一改动中同步该文档。

## 依赖方向

`apps` 依赖 `packages`；`packages` 之间的依赖只能自上而下（`protocols`/`datasources` → `chains` → `core`，
`storage` 只依赖 `core`）；`apps` 之间不互相依赖。新增一条链只需要实现 `alpha_chains` 的 `ChainAdapter` 接口，
新增一个协议只需要在 `alpha_protocols/plugins/` 下新增文件，下游不需要改动。
