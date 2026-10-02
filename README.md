# research

Alpha 系列的统一数据采集、特征、仿真与回测平台。只读不写链，不持有生产签名权限
（详见 [`AGENTS.md`](AGENTS.md) 的项目边界）。

**分层边界**：一个完整系统分五层——数据层/特征层/模型层/决策层/执行层。`research` 只做前三层
（采集数据、算特征、跑模型给出预测/打分），alpha-engine 仓库的 `products/alpha-lp` 做后两层（结合风控规则决策、
真正调用合约执行）。`research` 不碰私钥、不签名、不广播交易，也不复制 alpha-lp 的生产决策逻辑。
各层具体对应哪些模块，见 [`apps/lp-backtest/README.md`](apps/lp-backtest/README.md#分层架构这个系统在做什么不做什么)
的详细表格（其他 `apps/` 沿用同一套分层约定）。

采用单一 Python workspace（uv workspace），不是每个研究方向各建一个独立仓：`packages/` 放跨域共享的基建，
域相关的代码放各自的 `apps/`。详见 [`docs/lp-backtest-设计方案-v1.md`](docs/lp-backtest-设计方案-v1.md) 第 2 章。

```
research/
├── packages/                # 数据层/特征层/模型层基建，跨 apps 共享
│   ├── core/                  # 领域模型，无 I/O
│   ├── storage/                 # 唯一直接碰 DB 的地方（ORM + Alembic 迁移 + 仓储）
│   ├── chains/                   # 链适配器（get_logs/get_block/WebSocket 订阅统一接口）
│   ├── datasources/                # 外部数据源（GeckoTerminal/CoinGecko 等）
│   ├── protocols/                    # 协议插件（Factory 自动发现、Swap 事件解码、MasterChef 读取等）
│   └── metrics/                        # 特征层 + 模型层，lp-backtest/live-signal 两个 app 共用
│       └── src/alpha_metrics/
│           ├── features/                 # 特征层：确定性变换，不预测
│           ├── models/                    # 模型层：引入假设做预测/打分
│           ├── compute.py/assemble.py/scoring.py  # 组装：拼出一个池子/一批候选池的完整信号包
│           ├── chain_reads.py               # 特征/模型层需要的链上现场读取（feeProtocol/CAKE 排放）
│           └── snapshots.py                   # GeckoTerminal 历史快照回填，lp-backtest/live-signal 共用
└── apps/
    ├── lp-backtest/            # LP 池发现指标回归系统（历史回测/校准），见该目录 README
    │   └── src/lp_backtest/
    │       ├── discover.py/ingest.py/qualify.py  # 候选池发现 + 历史数据接入（1a）
    │       ├── register_pool.py            # 手动注册人工核实过的池子（跳过自动发现流程）
    │       ├── aggregate_candles.py          # 已废弃为定时数据来源（K 线现在走 live-signal
    │       │                                   轮询 GeckoTerminal），代码保留供手动交叉校验
    │       ├── validate/                       # 模型验证（1c/1d）
    │       └── calibrate/                        # 模型校准（1e）
    └── live-signal/             # 实时研究驱动决策系统（阶段 0）：只读结论 HTTP 服务 + 常驻数据管道
        └── src/live_signal/
            ├── main.py            # GET /conclusion?chain=bsc&pool=0x...；FastAPI lifespan 拉起后台任务
            └── background/          # WSS 逐笔订阅（默认关闭）/ K 线轮询 / 每日历史快照回填
```

## 本地开发

本项目用 [`uv`](https://docs.astral.sh/uv/) 管理 Python 依赖/虚拟环境/workspace（[安装](https://docs.astral.sh/uv/getting-started/installation/)：`curl -LsSf https://astral.sh/uv/install.sh | sh`），
不是 `pip`/`poetry`——`uv sync` 装依赖，`uv run <command>` 会自动确保虚拟环境和依赖是最新的再执行，
`packages/*`/`apps/*` 之间的内部依赖也是靠 uv 的 workspace 机制连起来的，不需要各自发布到 PyPI。

```bash
# 先起 LP 的 Postgres/Redis 实例（本仓库不另起库进程；假定 alpha-engine 与本仓库同级目录）
docker compose -f ../alpha-engine/products/alpha-lp/infra/docker-compose.yml up -d
docker compose up              # 在同一实例上创建 alpha_research 库（已存在则跳过）
cp .env.example .env           # 填 BNB_RPC_URLS；RESEARCH_DATABASE_URL 默认指向 127.0.0.1:5432/alpha_research
                                # 要跑 live-signal 还需要填 LIVE_SIGNAL_POOL_ADDRESSES（BNB_WSS_URL
                                # 只有启用 LIVE_SIGNAL_ENABLE_WSS_SUBSCRIBE 时才需要）
uv sync                        # 装全部 workspace 成员
(cd packages/storage && uv run alembic upgrade head)  # alembic.ini 的 script_location 是相对路径，需要在该目录下执行
```

表结构唯一文档真相：[`SCHEMA.md`](SCHEMA.md)。改表必须在同一改动中同步该文档。

## 依赖方向

`apps` 依赖 `packages`；`packages` 之间的依赖只能自上而下（`protocols`/`datasources` → `chains` → `core`，
`storage` 只依赖 `core`）；`apps` 之间不互相依赖。新增一条链只需要实现 `alpha_chains` 的 `ChainAdapter` 接口，
新增一个协议只需要在 `alpha_protocols/plugins/` 下新增文件，下游不需要改动。
