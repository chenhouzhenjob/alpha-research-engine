# 架构

## 目标

统一采集币圈 / 美股 / 基本面 / 链上 / 另类数据，供因子研究与回测。当前已落地：币安 USDT-M 永续与 Spot、Hyperliquid（perp OHLCV/funding/trades），Alpaca 美股 OHLCV / 历史 trades / quotes，Finnhub 公司基本面，FRED 宏观序列。

## 数据流

```text
integrations/venues/* (行情原生 API)
  → alpha.collection.RawArchive (JSONL)
  → alpha.collection.CanonicalStore (ohlcv / trade / quote / funding)
  → alpha.collection.Catalog (instruments / watermarks)
  → alpha.collection / DuckDB (研究读取)

integrations/providers 下的 Finnhub / Fred（独立协议，不实现 VenueAdapter）
  → RawArchive
  → CanonicalStore (fundamental | macro，分目录)
  → Catalog / load_fundamental | load_macro
```

存储相关代码均在 [`src/alpha/collection/storage/`](src/alpha/collection/storage/)：`canonical.py` / `catalog.py` / `query.py`。

## 可扩展约定

- 新交易所：在 [`src/alpha/integrations/venues/`](src/alpha/integrations/venues/) 新建 venue 包，并实现 `VenueAdapter`（见 [`market_data.py`](src/alpha/integrations/market_data.py)）。数据采集与将来的真实执行代码在同一 venue 包内共享 client，但由不同业务模块调用。
- 基本面/宏观：独立适配器 + `FundamentalPoint` / `MacroPoint`，勿硬塞进行情协议
- Canonical 信封：`schema_version`, `dataset`, `venue`, `market_type`, `instrument_id`, `symbol_raw`, `ts_event_ms`, `ts_ingest_ms`
- `dataset` / `venue` / `market_type` 为开放字符串，需要时再加新值与模型
- **表结构与字段解释**见 [`SCHEMA.md`](SCHEMA.md)

## 分区与目录含义

```text
data/canonical/{dataset}/venue={venue}/[tf={tf}/]date={YYYY-MM-DD}/part-000.parquet
data/raw/{venue}/{endpoint}/date={YYYY-MM-DD}/part.jsonl
data/catalog.sqlite
```

### `data/raw` vs `data/canonical`

| | [`data/raw/`](data/raw/) | [`data/canonical/`](data/canonical/) |
|--|--------------------------|--------------------------------------|
| 内容 | 交易所/接口**原始响应**归档 | 归一化后的研究表 |
| 格式 | JSONL（一行一条：`ts_ingest_ms` + `payload`） | Parquet（`ohlcv` / `trade` / `quote` / `funding` / `fundamental` / `macro`） |
| 用途 | 排障、复现解析、对照 Source Quirks | 因子、回测、日常查询 |
| 谁写 | adapter 在请求/WS 收到后写入 | 解析成 canonical 模型后由 `CanonicalStore` 写入 |

路径：`data/raw/{venue}/{endpoint}/date={YYYY-MM-DD}/part.jsonl`  
`endpoint` 来自接口名（如币安 `fapi_v1_klines`、Hyperliquid `info_candleSnapshot`、`ws_trade`）。

常见 raw 目录：

| venue | endpoint | 含义 |
|-------|----------|------|
| alpaca | `v2_stocks_*_bars` | 美股 K 线 |
| alpaca | `v2_stocks_*_trades` | 美股历史成交 |
| alpaca | `v2_stocks_*_quotes` | 美股 L1 报价 |
| alpaca | `v2_assets` | 资产列表校验（可选） |
| finnhub | `stock_financials` / `stock_metric` / `stock_earnings` | 公司报表与指标 |
| fred | `series_observations` | 宏观观测序列 |
| binance | `fapi_v1_exchangeInfo` | 合约元数据 |
| binance | `fapi_v1_klines` | K 线原始数组 |
| binance | `fapi_v1_fundingRate` | 资金费率历史 |
| binance | `fapi_v1_premiumIndex` | 标记价/溢价 |
| binance | `ws_trade` | WS 逐笔成交 |
| hyperliquid | `info_metaAndAssetCtxs` | 市场元数据 |
| hyperliquid | `info_candleSnapshot` | K 线 |
| hyperliquid | `info_fundingHistory` | 资金费率 |
| hyperliquid | `ws_trades` | WS 成交 |

日常分析用 **canonical**；怀疑解析错误或核对原始字段时再翻 **raw**。

[`data/catalog.sqlite`](data/catalog.sqlite) 存 `instruments`（标的注册）与 `watermarks`（各 dataset 采集水位）。

### 如何查看数据

**Python（推荐）**

```python
from alpha.collection import load_ohlcv, load_funding, load_fundamental, load_macro, load_trades, load_quote

load_funding("data", venue="binance")
load_ohlcv("data", venue="binance", tf="1h")
load_trades("data", venue="alpaca", symbol_raw="AAPL")
load_quote("data", venue="alpaca", symbol_raw="AAPL")
load_fundamental("data", venue="finnhub", symbol_raw="AAPL")
load_macro("data", venue="fred", symbol_raw="GDP")
```

**DuckDB 读单个 Parquet**

```python
import duckdb
duckdb.sql("""
  SELECT * FROM read_parquet(
    'data/canonical/funding/venue=binance/date=2026-07-30/part-000.parquet'
  )
""").show()
```

**DBX**（[t8y2/dbx](https://github.com/t8y2/dbx)：支持拖拽预览 Parquet，底层 DuckDB）

1. 新建连接 → 类型选 **DuckDB**
2. 文件路径填 `:memory:`
3. 初始化脚本示例（改成你的绝对路径）：

```sql
CREATE OR REPLACE VIEW funding AS
SELECT * FROM read_parquet(
  '/Users/chenhouzhen/project/codes/bots/alpha-research-engine/data/canonical/funding/**/*.parquet',
  hive_partitioning := true
);

CREATE OR REPLACE VIEW ohlcv AS
SELECT * FROM read_parquet(
  '/Users/chenhouzhen/project/codes/bots/alpha-research-engine/data/canonical/ohlcv/**/*.parquet',
  hive_partitioning := true
);

CREATE OR REPLACE VIEW trades AS
SELECT * FROM read_parquet(
  '/Users/chenhouzhen/project/codes/bots/alpha-research-engine/data/canonical/trade/**/*.parquet',
  hive_partitioning := true
);

CREATE OR REPLACE VIEW quote AS
SELECT * FROM read_parquet(
  '/Users/chenhouzhen/project/codes/bots/alpha-research-engine/data/canonical/quote/**/*.parquet',
  hive_partitioning := true
);

CREATE OR REPLACE VIEW fundamental AS
SELECT * FROM read_parquet(
  '/Users/chenhouzhen/project/codes/bots/alpha-research-engine/data/canonical/fundamental/**/*.parquet',
  hive_partitioning := true
);

CREATE OR REPLACE VIEW macro AS
SELECT * FROM read_parquet(
  '/Users/chenhouzhen/project/codes/bots/alpha-research-engine/data/canonical/macro/**/*.parquet',
  hive_partitioning := true
);
```

4. 保存并连接后：`SELECT * FROM funding LIMIT 20;`
5. 看 catalog：另建 **SQLite** 连接，指向 `data/catalog.sqlite`
6. 也可直接把单个 `.parquet` 拖进 DBX 预览

**说明**：Parquet 是列存二进制，不能当文本直接打开；raw 的 `.jsonl` 可用编辑器打开。

## 远期路线（仅文档）

| Phase | 内容 |
|-------|------|
| 1 | Binance + Hyperliquid：OHLCV / funding / trades |
| 2 | 美股 OHLCV：Alpaca（已接入）；后续可加 Polygon |
| 3 | 美股基本面 Finnhub + 宏观 FRED（已接入；分目录 `fundamental` / `macro`） |
| 4 | 链上：DefiLlama / Dune / Glassnode / Artemis |
| 5 | 另类 + 因子/回测读口 |

与 [`crypto-bots/`](../crypto-bots/) 解耦：本仓做研究数据，不负责套利扫描与下单。

## Source Quirks

### Binance USDT-M

1. **K 线时间**：`klines` 第一列为 **openTime**；canonical `ts_event_ms` 使用 openTime，不是 closeTime。
2. **分页 limit**：单次最多 1500 根；需按 `openTime + interval` 推进，否则可能死循环或漏段。
3. **限流**：权重制；429/418 需退避。公开行情接口通常无需 API Key，但 IP 仍受限。
4. **符号**：永续为 `BTCUSDT`（无斜杠）；与 Hyperliquid 的 `BTC` 不同，跨所对齐靠后续 asset 映射，Phase 1 不强行统一。
5. **资金费率**：`/fapi/v1/fundingRate` 的 `fundingRate` 已是小数；`markPrice` 字段并非每条历史都有。
6. **成交流**：USDT-M `fstream` 上 `@aggTrade` 可能长时间无推送（本环境实测连接成功但 12s 无消息）；`@trade` 正常。适配器默认订阅 `@trade`，解析兼容 `a`（agg）与 `t`（逐笔）作为 `trade_id`。

### Binance Spot

1. **共享 source 配置**：Spot 与永续 profile 都在 [`binance.yaml`](configs/collection/sources/binance.yaml)；采集时分别使用 `binance_spot` 与 `binance_perp` source，但 canonical `venue` 均为 `binance`，以 `market_type` 分开。
2. **端点**：REST 为 `/api/v3`，WebSocket 为 `stream.binance.com`；不得复用永续合约的 `fapi` / `fstream` 地址。
3. **无 funding**：`alpha collect sync funding` 会自动跳过现货 source。
4. **标识**：例如现货为 `binance:spot:BTCUSDT`，永续为 `binance:perp:BTCUSDT`；两者可同名但不可混淆。

### Hyperliquid

1. **历史深度**：`candleSnapshot` 官方文档称大约只保留最近 **5000** 根；深历史回填会静默截断，watermark 不能假设「已拉到 start_ms」。
2. **API 形态**：Info 为单一 `POST /info`，用 body `type` 区分能力（与币安多 path REST 不同）。
3. **符号**：`coin` 为 `BTC` / `ETH`，不是 `BTCUSDT`；数值多为 **字符串**，解析时需 `float()`。
4. **K 线字段**：开盘 `t`、收盘 `T`、周期 `i`、成交笔数 `n`；canonical 用 `t` 作 `ts_event_ms`。
5. **成交 WS**：`side` 常见 `B`/`A`（买/卖），需映射到 `buy`/`sell`；去重键优先 `tid`，否则 `hash`。

### Alpaca（美股）

1. **鉴权**：密钥只放仓库根目录 `.env`（`ALPACA_API_KEY` / `ALPACA_API_SECRET`，见 [`.env.example`](.env.example)）；[`alpaca.yaml`](configs/collection/sources/alpaca.yaml) 只含标的/周期等非密钥配置。
2. **feed**：默认 **`sip`**（全市场）；同时作用于 bars / quotes / trades。`iex` 为免费 IEX 单所。免费档查询 SIP 时 **end 须距现在 ≥约 15 分钟**（适配器截到 now−16m）。
3. **adjustment**：默认 `split`（拆分复权，仅 bars）；可选 `raw` / `dividend` / `all`。
4. **周期映射**：`1m`→`1Min`，`1h`→`1Hour`，`1d`→`1Day` 等；`ts_event_ms` 用 bar 起始时间 `t`（RFC3339）。
5. **历史 trades / quotes**：`GET .../trades` 与 `.../quotes`；CLI `alpha collect backfill trade|quote --venue alpaca`（默认近 1 天，体量大）。无资金费率；**实时 WS 成交流本阶段未接**（`collect stream trades` 对 alpaca 仍跳过）。
6. **instrument_id**：`alpaca:stock:AAPL`（canonical 用 `stock`，Alpaca assets API 参数名仍为 `us_equity`）。

### Finnhub（公司基本面）

1. **鉴权**：`.env` 中 `FINNHUB_API_KEY`；配置见 [`configs/collection/sources/finnhub.yaml`](configs/collection/sources/finnhub.yaml)。
2. **限流**：免费约 60 次/分钟；适配器默认按 50/min 节流。
3. **免费档接口**：`/stock/metric`（关键指标）、`/stock/earnings`（盈利意外）。标准化报表 `/stock/financials` **通常需付费**，免费调用会 **403**；默认 `fetch_financials: false`。
4. **`ts_event_ms`**：盈利用报告期期末；metric 快照用拉取日 UTC 0 点；若开启 financials 则用报告期期末。
5. **instrument_id**：`finnhub:stock:AAPL`，与 Alpaca 行情 ID 分离，研究侧用 `symbol_raw` 对齐。
6. **许可**：免费档多为 Personal Use，商用需自行核对条款。

### FRED（宏观）

1. **鉴权**：`.env` 中 `FRED_API_KEY`（[申请](https://fred.stlouisfed.org/docs/api/api_key.html)）；配置见 [`configs/collection/sources/fred.yaml`](configs/collection/sources/fred.yaml)。
2. **接口**：`series/observations`；缺测值 `"."` 跳过。
3. **修订**：FRED 常修订历史点；以**最近一次拉取**覆盖去重键上的旧值。
4. **instrument_id**：`fred:macro:{SERIES_ID}`，`metric` 固定 `value`。

### 对比小结（为何不用 CCXT / 官方 SDK）

| 点 | Binance | Hyperliquid | Alpaca | Finnhub | FRED |
|----|---------|-------------|--------|---------|------|
| 符号 | BTCUSDT | BTC | AAPL | AAPL | GDP 等 |
| market_type | perp | perp | stock | stock | macro |
| 主数据 | K 线/费率/成交 | 同左 | K 线 + 历史 trade/quote | 财报长表 | 宏观观测 |
| 鉴权 | 行情通常无需 | 无需 | API Key+Secret | API Key | API Key |
