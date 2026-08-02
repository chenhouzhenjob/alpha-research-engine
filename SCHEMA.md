# 表结构与字段说明

模型定义见 `[src/alpha/schema.py](src/alpha/schema.py)`；catalog DDL 见 `[src/alpha/collection/storage/catalog.py](src/alpha/collection/storage/catalog.py)`。  
当前 `schema_version = 1`（字段不兼容变更时递增）。

## 存储位置


| 数据集                      | 路径                                                           | 格式      |
| ------------------------ | ------------------------------------------------------------ | ------- |
| ohlcv                    | `[data/canonical/ohlcv/](data/canonical/ohlcv/)`             | Parquet |
| trade                    | `[data/canonical/trade/](data/canonical/trade/)`             | Parquet |
| quote                    | `[data/canonical/quote/](data/canonical/quote/)`             | Parquet |
| funding                  | `[data/canonical/funding/](data/canonical/funding/)`         | Parquet |
| fundamental              | `[data/canonical/fundamental/](data/canonical/fundamental/)` | Parquet |
| macro                    | `[data/canonical/macro/](data/canonical/macro/)`             | Parquet |
| instruments / watermarks | `[data/catalog.sqlite](data/catalog.sqlite)`                 | SQLite  |
| 原始接口响应                   | `[data/raw/](data/raw/)`                                     | JSONL   |


分区约定：

```text
data/canonical/{dataset}/venue={venue}/[tf={tf}/]date={YYYY-MM-DD}/part-000.parquet
data/raw/{venue}/{endpoint}/date={YYYY-MM-DD}/part.jsonl
```

---



## 公共信封（Envelope）

所有 canonical 行（ohlcv / trade / quote / funding / fundamental / macro）共享以下字段。


| 字段               | 类型   | 说明                                                                                        |
| ---------------- | ---- | ----------------------------------------------------------------------------------------- |
| `schema_version` | int  | 行级 schema 版本，当前为 `1`                                                                      |
| `dataset`        | str  | 数据集名：`ohlcv` / `trade` / `quote` / `funding` / `fundamental` / `macro`                    |
| `venue`          | str  | 数据源，如 `binance`、`hyperliquid`、`alpaca`、`finnhub`、`fred`                                   |
| `market_type`    | str  | 市场类型：`perp` / `stock` / `macro`                                                           |
| `instrument_id`  | str  | 单源稳定 ID：`{venue}:{market_type}:{symbol_raw}`，例 `binance:perp:ETHUSDT`、`alpaca:stock:AAPL` |
| `symbol_raw`     | str  | 源站原始符号（排障用），勿假设跨所相同                                                                       |
| `ts_event_ms`    | int  | 事件时间（交易所钟，UTC 毫秒）                                                                         |
| `ts_ingest_ms`   | int  | 本机入库时间（UTC 毫秒）                                                                            |
| `source_seq`     | str? | 可选去重/追溯键（如成交 ID、K 线 openTime）                                                             |


**跨所注意**：`instrument_id` 不跨 venue 合并。币安 ETH 永续为 `ETHUSDT`（quote/settle=USDT），Hyperliquid 为 `ETH`（settle=USDC）。价格多数时候接近，但结算币与符号不同，研究跨所对比时需自行映射，不能当同一合约硬拼。

---



## `ohlcv`（K 线）


| 字段             | 类型     | 说明                      |
| -------------- | ------ | ----------------------- |
| （Envelope 全部）  |        | 见上；`dataset` 固定 `ohlcv` |
| `tf`           | str    | 周期，如 `1m`、`1h`、`1d`     |
| `open`         | float  | 开盘价                     |
| `high`         | float  | 最高价                     |
| `low`          | float  | 最低价                     |
| `close`        | float  | 收盘价                     |
| `volume`       | float  | 成交量（标的数量，base）          |
| `trade_count`  | int?   | 成交笔数（若源提供）              |
| `quote_volume` | float? | 成交额（计价币，若源提供）           |


- `ts_event_ms`：取 K 线**开盘时间**（Binance `openTime` / Hyperliquid `t`），不是收盘时间。  
- **去重键**：`(instrument_id, ts_event_ms, tf)`  
- **分区**：含 `tf=` 与 `date=`（按 `ts_event_ms` 的 UTC 日）

---



## `trade`（成交）


| 字段               | 类型    | 说明                                                            |
| ---------------- | ----- | ------------------------------------------------------------- |
| （Envelope 全部）    |       | 见上；`dataset` 固定 `trade`                                       |
| `price`          | float | 成交价                                                           |
| `size`           | float | 成交数量（base）                                                    |
| `side`           | str   | `buy` / `sell` / `unknown`（主动方向；币安由 `m` 推断，HL 由 `B`/`A` 映射）   |
| `trade_id`       | str   | 源站成交 ID（币安 `@trade` 用 `t`，agg 用 `a`；HL 优先 `tid`；Alpaca 用 `i`） |
| `is_buyer_maker` | bool? | 买方是否为 maker（币安有；HL / Alpaca 历史通常无）                            |
| `feed`           | str?  | 美股数据源 `iex` / `sip`；crypto 通常为空                               |


- `ts_event_ms`：成交发生时间。  
- **去重键**：`(instrument_id, trade_id)`  
- **分区**：`venue=` + `date=`（无 `tf`）

---



## `quote`（L1 买卖一档）


| 字段            | 类型    | 说明                      |
| ------------- | ----- | ----------------------- |
| （Envelope 全部） |       | 见上；`dataset` 固定 `quote` |
| `bid_px`      | float | 买一价                     |
| `bid_sz`      | float | 买一量                     |
| `ask_px`      | float | 卖一价                     |
| `ask_sz`      | float | 卖一量                     |
| `feed`        | str   | `iex` / `sip`           |


- `ts_event_ms`：报价事件时间（高精度截断到毫秒）。  
- **去重键**：`(instrument_id, ts_event_ms, bid_px, ask_px, bid_sz, ask_sz, feed)`  
- **分区**：`venue=` + `date=`（无 `tf`）  
- 与 `trade` **分目录**；价差 / 中间价查询时计算即可。

---



## `funding`（资金费率）


| 字段                   | 类型     | 说明                                  |
| -------------------- | ------ | ----------------------------------- |
| （Envelope 全部）        |        | 见上；`dataset` 固定 `funding`           |
| `funding_rate`       | float  | 资金费率，**小数**（`0.0001` = 1bp = 0.01%） |
| `mark_price`         | float? | 标记价格（若有）                            |
| `index_price`        | float? | 指数价格（若有）                            |
| `next_funding_ts_ms` | int?   | 下次结算时间（若有）                          |


- `ts_event_ms`：该笔费率对应的结算/记录时间。  
- **去重键**：`(instrument_id, ts_event_ms)`  
- 费率单位已在各 adapter 内对齐为小数；币安与 HL 结算周期可能不同，对比时需留意。

---



## `fundamental`（公司基本面，长表）

与 `macro` **分目录存储**，模型为 `FundamentalPoint`。


| 字段            | 类型    | 说明                                                            |
| ------------- | ----- | ------------------------------------------------------------- |
| （Envelope 全部） |       | `dataset=fundamental`；`venue` 如 `finnhub`；`market_type=stock` |
| `metric`      | str   | 指标名（源字段或归一化名，如 `revenue`、`pe_ttm`）                            |
| `value`       | float | 数值                                                            |
| `unit`        | str?  | 如 `USD` / `ratio` / `percent`                                 |
| `frequency`   | str   | `annual` / `quarterly` / `ttm` 等                              |
| `statement`   | str   | `ic` / `bs` / `cf` / `metric` / `earnings`                    |


- `ts_event_ms`：报告期**期末**日期的 UTC 0 点（Finnhub `period`）。关键指标快照用拉取当日 UTC 日起点。  
- **去重键**：`(instrument_id, metric, statement, frequency, ts_event_ms)`  
- **分区**：`venue=` + `date=`（无 `tf`）  
- **instrument_id**：`finnhub:stock:AAPL`（不与 `alpaca:stock:AAPL` 合并；研究用 `symbol_raw` 对齐）

---



## `macro`（宏观指标，长表）

与 `fundamental` **分目录存储**，模型为 `MacroPoint`（无 `statement` 字段）。


| 字段            | 类型    | 说明                                                   |
| ------------- | ----- | ---------------------------------------------------- |
| （Envelope 全部） |       | `dataset=macro`；`venue` 如 `fred`；`market_type=macro` |
| `metric`      | str   | 固定为 `value`（序列身份在 `symbol_raw` / `instrument_id`）    |
| `value`       | float | 观测值                                                  |
| `unit`        | str?  | 来自配置，如 `percent` / `billions_usd`                    |
| `frequency`   | str   | `quarterly` / `monthly` 等                            |


- `ts_event_ms`：观测期日期（FRED `date`）的 UTC 0 点。  
- **去重键**：`(instrument_id, metric, frequency, ts_event_ms)`  
- **分区**：`venue=` + `date=`  
- **instrument_id**：`fred:macro:GDP`

---



## Catalog：`instruments`

路径：`[data/catalog.sqlite](data/catalog.sqlite)` 表 `instruments`。


| 字段              | 类型       | 说明                                      |
| --------------- | -------- | --------------------------------------- |
| `instrument_id` | TEXT PK  | 同 Envelope                              |
| `venue`         | TEXT     | 数据源                                     |
| `market_type`   | TEXT     | 如 `perp`                                |
| `base`          | TEXT     | 标的资产，如 `ETH`                            |
| `quote`         | TEXT     | 报价资产：币安多为 `USDT`；HL 现为 `USD`            |
| `settle`        | TEXT?    | 结算/保证金资产：币安 USDT-M 为 `USDT`；HL 为 `USDC` |
| `symbol_raw`    | TEXT     | 源站符号                                    |
| `listed_at`     | INTEGER? | 上市时间 ms（若有）                             |
| `delisted_at`   | INTEGER? | 下市时间 ms（若有）                             |
| `meta_json`     | TEXT     | 源站额外元数据 JSON 字符串（精度、杠杆等）                |


示例：


| instrument_id          | base | quote | settle |
| ---------------------- | ---- | ----- | ------ |
| `binance:perp:ETHUSDT` | ETH  | USDT  | USDT   |
| `hyperliquid:perp:ETH` | ETH  | USD   | USDC   |
| `alpaca:stock:AAPL`    | AAPL | USD   | USD    |


---



## Catalog：`watermarks`

采集水位，用于增量回填，只增不减。


| 字段              | 类型      | 说明                                                                  |
| --------------- | ------- | ------------------------------------------------------------------- |
| `venue`         | TEXT    | 数据源                                                                 |
| `dataset`       | TEXT    | `ohlcv` / `funding` / `trade` / `quote` / `fundamental` / `macro` 等 |
| `instrument_id` | TEXT    | 标的                                                                  |
| `tf`            | TEXT    | K 线周期；非 ohlcv 时多为空串 `''`                                            |
| `ts_ms`         | INTEGER | 已成功写入的最大事件时间（ms）                                                    |


主键：`(venue, dataset, instrument_id, tf)`。

---



## Raw JSONL（非表，归档行）

每行一个 JSON 对象：


| 字段             | 说明                               |
| -------------- | -------------------------------- |
| `ts_ingest_ms` | 写入 raw 的时间                       |
| `payload`      | 接口原始响应（或请求参数 + 响应，视 endpoint 而定） |


不参与研究 schema；仅供排障与对照解析。目录说明见 `[ARCHITECTURE.md](ARCHITECTURE.md)`。