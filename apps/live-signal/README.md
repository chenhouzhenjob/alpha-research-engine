# live-signal

实时研究驱动决策系统 · **阶段 1** 的只读结论 HTTP 服务。设计方案见
[`../../docs/live-signal-system-设计方案.md`](../../docs/live-signal-system-设计方案.md)。

## 这是什么

`alpha-lp` 定时轮询本服务的 `GET /conclusion` 端点获得决策结论——调用方向是 alpha-lp 主动拉取，
不是 research 主动推送。这一层只负责"把 research 侧算出的信号以稳定的 HTTP 接口暴露出去"，
决策/执行逻辑不在这里（见根 [`AGENTS.md`](../../AGENTS.md) 的项目边界）。

响应里 `model_version` 当前是 `live-signal-v0.1.0-phase1`：

- `exit_signals`（5 条退出信号，见 [`../../packages/metrics/src/alpha_metrics/models/README.md`](../../packages/metrics/src/alpha_metrics/models/README.md)
  第 5 节）和 `recommended_range`（同文档第 4 节）已经是真实计算，不再是阶段 0 的占位。
- `recommended_action`：5 条退出信号任意一条"可用且触发" → `exit`；否则退回复合分 `tier`
  简单映射（S/A → `hold`，B/C → `no_signal`）。**不返回 `rebalance`**——research 不知道调用方
  当前仓位的实际 tick 范围，"现在的区间跟推荐的差多少、值不值得为了 gas 成本去调"这个比较只有
  调用方自己能做，`recommended_range` 给的就是这个判断所需的数据。
- `risk_flags` 仍然恒为 `{}`——RWA 参考价数据源、TrackingError 等特征是阶段 2 的事，这一项
  在响应里保留占位。

## 启动步骤

第一次跑（或者换了台机器）需要先把 DB/依赖/迁移准备好，跟 [`../../README.md`](../../README.md)
"本地开发"一节是同一套步骤，这里按 live-signal 需要的顺序完整列一遍：

```bash
# 1. Postgres（跟 alpha-lp 共用同一个实例，只是库名不同）
docker compose -f ../../../alpha-engine/products/alpha-lp/infra/docker-compose.yml up -d   # alpha-engine 与本仓库同级
cd ../..                        # 回到仓库根目录
docker compose up               # 在同一实例上创建 alpha_research 库（已存在则跳过）

# 2. 配置
cp .env.example .env            # 没有的话先复制一份
# 编辑 .env：RESEARCH_DATABASE_URL 默认就指向刚起的实例，一般不用改；
# BNB_RPC_URLS 至少填一个能用的 BSC RPC（免费可用 PublicNode，见 .env.example 注释）；
# LIVE_SIGNAL_POOL_ADDRESSES 必填——没有会导致 /conclusion 直接 500，见下方"候选集"一节。
# 池子要先注册进 pool_candidates 才能被识别，见下面"注册新池子"。

# 3. 装依赖 + 建表
uv sync --all-packages
(cd packages/storage && uv run alembic upgrade head)   # script_location 是相对路径，必须在这个目录下执行

# 4. 起服务——K 线轮询、历史快照回填两个后台任务随服务启动自动开始，不需要额外操作
uv run live-signal-serve         # 监听 0.0.0.0:8100，Ctrl+C 停止（会等后台任务优雅退出）
```

另开一个终端验证：

```bash
curl "http://127.0.0.1:8100/conclusion?chain=bsc&pool=0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4"
```

`pool` 必须是 `LIVE_SIGNAL_POOL_ADDRESSES` 里配置的地址，否则返回 404；
`LIVE_SIGNAL_POOL_ADDRESSES` 本身未配置时返回 500（要求显式配置候选集，不悄悄回退到
全部 `status=qualified` 候选池——原因见下）。首次请求因为要现场读链上状态、没有缓存，
大约 20-30 秒才会返回，是正常现象，见下方"性能"一节。

### 可选 query 参数：仓位基线（退出信号 3/5 用）

5 条退出信号里有 2 条（VTRatio 枯竭、累计已实现 IL 超过累计手续费）需要"开仓那一刻"的基线值——
research 不追踪仓位，这些值必须由调用方（如 alpha-lp，它自己有 `positions` 表）传入：

| 参数 | 类型 | 说明 |
|---|---|---|
| `position_open_vtratio_ma7` | float，可选 | 开仓时 VTRatio 7日均值——不传则 `vtratio_ma7_drop_pct` 为 `null` |
| `position_open_price` | float，可选 | 开仓时 token1/token0 汇率——不传则信号 5 两个字段为 `null` |
| `position_open_value_usd` | float，可选 | 开仓时头寸美元价值——不传则信号 5 两个字段为 `null` |
| `position_cumulative_fees_usd` | float，可选 | 累计已实现手续费+CAKE收入（美元）——不传则信号 5 两个字段为 `null` |

不传这 4 个参数时，`/conclusion` 仍然正常返回，只是这两条信号标记不可用（`null`），不阻塞其余
3 条池子级别信号（NetEdge、CAKE 撤出、相对排名坍塌）。

### 注册新池子

`LIVE_SIGNAL_POOL_ADDRESSES` 里的地址必须先出现在 `pool_candidates` 表里（`status=qualified`），
否则 `/conclusion` 也会返回 404。用这个 CLI 手动注册（不用跑 Factory 全量扫描/准入判定）：

```bash
uv run lp-backtest-register-pool \
  --pool-address 0x... --token0 0x... --token1 0x... \
  --fee-pips 500 --tick-spacing 10 --asset-class crypto_native
```

`--asset-class` 是 `crypto_native`（加密原生资产对）或 `rwa`（锚定真实世界资产，如 QQQB）。

## 候选集为什么要单独配置，而不是复用 `pool_candidates` 全部 `qualified` 池子

复合分（`composite_score`）是候选集内的相对排名（min-max 归一化），不是绝对分数——`calibrate/
weights` 那套校准脚本的语义是"DB 里全部 `status=qualified` 的池子"。但 `pool_candidates` 混杂了
两种不同目的的池子：1a-1f 阶段自动发现/校准用的历史候选池（当前有 28 个），和这次阶段 0 手动
注册的实时试跑池子（`lp-backtest-register-pool` 注册的 2 个）。

真实跑过一次就会发现：直接复用"全部 qualified"当候选集，单次 `/conclusion` 请求要对 28 个池子
各查好几次链上状态（`feeProtocol` + CAKE 排放），耗时超过 3 分钟——这个端点设计上是要被
alpha-lp 高频轮询的，3 分钟一次完全不可用；而且拿一堆无关的历史测试池子来做相对排名对比，
语义上也没有意义。所以候选集必须显式配置，见 `LIVE_SIGNAL_POOL_ADDRESSES`。

## 性能：链上状态读取已加缓存

`feeProtocol`/CAKE 排放这两项链上状态最早是每次 `/conclusion` 请求都现场 `eth_call` 重查——
这两个值实际变化很慢（`feeProtocol` 是治理参数几乎不变，CAKE 排放速率通常按周/双周周期变），
高频轮询这个端点会造成大量没必要的链上调用，真实跑过才发现，见 `alpha_metrics.chain_reads`
的 5 分钟 TTL 缓存（进程内，`live-signal` 常驻进程跨请求共享）。实测：无缓存 ~20-30 秒/请求，
缓存命中后 ~1-1.5 秒/请求。`decimals()`（ERC20 精度，不可变值）另有独立的永久缓存（`tokens`
表），不受 TTL 限制。

## 数据链路

三个数据管道都是 [`background/`](src/live_signal/background) 下的 asyncio 常驻任务，
由 `main.py` 的 FastAPI `lifespan` 在应用启动时 `asyncio.create_task` 拉起，跟 HTTP
请求处理共用同一个进程/事件循环——不是分开部署的 CLI，也不由 crontab 触发（阶段 0 之前
用过 crontab + 独立 CLI 的方案，已经废弃，见下方"部署注意事项"一节）：

1. [`background/subscribe.py`](src/live_signal/background/subscribe.py)：WebSocket 逐笔
   订阅（默认端点 PublicNode，免费），把 `Swap` 事件落库到 `swap_events`——不是为了喂给
   K 线，是为了保留逐笔明细供以后的风控信号（如大户集中度、刷量模式识别）用。**默认关闭**
   （见下方环境变量），K 线已经不依赖它。
2. [`background/poll_ohlcv.py`](src/live_signal/background/poll_ohlcv.py)：每 60 秒轮询
   GeckoTerminal 的分钟级 OHLCV 接口，直接写入 `pool_ohlcv` 表——这条路径**不经过**
   `swap_events`：早期方案是自己订阅 Swap 事件 + 现场聚合（`apps/lp-backtest` 的
   `aggregate_candles.py`，仍保留代码但不再是数据来源），但 GeckoTerminal 自己的索引
   管线已经把这件事做了（实测延迟约 2 分钟），直接轮询比自建聚合简单得多。`/conclusion`
   现在会消费 `pool_ohlcv`（重采样成 5 分钟K线算 ATR/ADX，见 `packages/metrics` 的
   `models/README.md` 第 4 节）——刚启动、还没攒够 ADX 需要的 ~140 根 1 分钟K线（约 2.5 小时）
   时，`recommended_range` 会正确返回 `null`，这是预期行为，不是 bug。
3. [`background/ingest.py`](src/live_signal/background/ingest.py)：每天一次，把
   GeckoTerminal 的收盘价历史 + 当前 TVL/24h volume 写入 `pool_metrics_history`，复用
   `alpha_metrics.snapshots.backfill_snapshots`（跟 `lp-backtest-ingest` 共用同一份逻辑，
   那边服务 1a-1f 阶段的历史候选池，这边只服务 `LIVE_SIGNAL_POOL_ADDRESSES` 配置的池子）。
4. `/conclusion` 复用 `packages/metrics`（`alpha_metrics.assemble`/`alpha_metrics.scoring`）
   算出的 `PoolDailyMetrics` + `CompositeScoreResult`，和 `lp-backtest` 的历史回测/校准共用
   同一套特征/模型代码，不重复实现。

## 部署注意事项

不需要 crontab——三个数据管道都在 `live-signal-serve` 这一个进程里，启动这一个命令就够了
（见上方"启动步骤"）。阶段 0 明确不做进程级"保活"（崩溃自动重启）：这个进程本身的存活
交给外部工具（systemd/launchd，或者手动重启），不在应用代码范围内；后面如果要跑多个
`live-signal` 实例（横向扩容），`background/subscribe.py` 这类"必须只有一个实例真正执行"
的任务需要引入分布式锁（如 Postgres advisory lock）协调，这次不做。

**环境变量参考**（除 `LIVE_SIGNAL_POOL_ADDRESSES` 必填外都有默认值，见根目录 `.env.example`）：

| 变量 | 必填 | 说明 |
|---|---|---|
| `RESEARCH_DATABASE_URL` | 是 | Postgres 连接串 |
| `BNB_RPC_URLS` | 是 | 逗号分隔，第一个为主 RPC，其余故障转移备用 |
| `LIVE_SIGNAL_POOL_ADDRESSES` | 是 | `/conclusion` 候选集，逗号分隔的池子地址 |
| `BNB_LOG_CHUNK_SIZE` | 否 | 单次 `eth_getLogs` 区块跨度上限，换 RPC 供应商报错时调 |
| `BNB_WSS_URL` | 仅 WSS 订阅需要 | 只有 `LIVE_SIGNAL_ENABLE_WSS_SUBSCRIBE=1` 时才会用到 |
| `LIVE_SIGNAL_ENABLE_WSS_SUBSCRIBE` | 否 | 设为 `1`/`true` 才会启动 WebSocket 逐笔订阅任务，默认不启动（K 线不依赖它，先省 WS 流量成本——多数 WS 供应商按字节计费，见部署记录） |
