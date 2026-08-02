# alpha-research-engine

多源研究数据湖：原生对接交易所/数据商，统一 canonical schema，供 AI 挖因子与回测读取。

## 当前范围

- 币安 USDT-M 永续、币安 Spot、Hyperliquid：OHLCV / 实时 trades；funding 仅永续合约
- **Alpaca 美股**：OHLCV、历史 trades、L1 quotes（默认 `feed: sip`）
- **Finnhub**：公司基本面 → `data/canonical/fundamental/`
- **FRED**：宏观序列 → `data/canonical/macro/`（与公司基本面分目录）
- **不使用 CCXT**，直连原生 REST/WS
- 存储：raw JSONL + canonical Parquet + SQLite catalog

## 安装

```bash
cd alpha-research-engine
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 常用命令

```bash
# 刷新标的
alpha collect catalog refresh-instruments

# 回填币圈 1h K 线
alpha collect backfill ohlcv --venue binance,hyperliquid --tf 1h --days 7
alpha collect backfill ohlcv --venue binance --tf 1h --start 2024-01-01 --end 2024-02-01
# Binance 默认同时回填 perp 与 spot；可按 market_type 只选现货
alpha collect backfill ohlcv --venue binance --market-type spot --tf 1h --days 7
# 忽略已有采集水位，强制重拉指定历史区间（仍会按 canonical 业务键去重）
alpha collect backfill ohlcv --venue binance --market-type perp --tf 1h --start 2024-01-01 --end 2024-02-01 --force

# 回填美股日线（先配置 .env 中的 ALPACA_* 密钥）
alpha collect catalog refresh-instruments --venue alpaca
alpha collect backfill ohlcv --venue alpaca --tf 1d --days 30

# 美股历史成交 / L1 报价（体量大；可按月增量，水位会接着拉）
alpha collect backfill trade --venue alpaca --days 1
alpha collect backfill quote --venue alpaca --days 1
alpha collect backfill trade --venue alpaca --start 2024-01-01 --end 2024-02-01
alpha collect backfill quote --venue alpaca --start 2026-07-25 --end 2026-08-01

# 公司基本面（FINNHUB_API_KEY）
alpha collect catalog refresh-instruments --venue finnhub
alpha collect backfill fundamental --venue finnhub
alpha collect backfill fundamental --venue finnhub --start 2020-01-01 --end 2025-01-01

# 宏观序列（FRED_API_KEY）
alpha collect catalog refresh-instruments --venue fred
alpha collect backfill macro --venue fred
alpha collect backfill macro --venue fred --series GDP --start 2006-07-25 --end 2026-08-01

# 同步资金费率（仅 crypto）
alpha collect sync funding --days 7
alpha collect sync funding --start 2024-01-01 --end 2024-07-01

# 订阅成交（仅 crypto）
alpha collect stream trades --duration 60
```

### Watermark 与强制重采

OHLCV、历史成交（`trade`）、L1 报价（`quote`）、公司基本面（`fundamental`）、宏观序列（`macro`）的回填，以及资金费率（`funding`）同步，默认都从对应 `watermark` 之后开始，避免重复请求。为重拉历史、修复解析或获取宏观/基本面修订值，可为这些带时间窗的命令添加 `--force`。

```bash
alpha collect backfill trade --venue alpaca --days 1 --force
alpha collect backfill quote --venue alpaca --days 1 --force
alpha collect backfill fundamental --venue finnhub --start 2020-01-01 --end 2025-01-01 --force
alpha collect backfill macro --venue fred --series GDP --start 2020-01-01 --force
alpha collect sync funding --venue binance --market-type perp --days 30 --force
```

`--force` 仅忽略**本次**读取的 watermark：请求仍受 `--start`、`--end`、`--days` 限制，写入仍按 canonical 业务键去重，且任务成功后会更新 watermark。

采集配置见 [`configs/collect.yaml`](configs/collect.yaml) 与 [`configs/collection/sources/`](configs/collection/sources/)。

密钥统一放在仓库根目录 `.env`（已被 gitignore）：

```bash
cp .env.example .env
# 编辑 .env 填写 ALPACA_* / FINNHUB_API_KEY / FRED_API_KEY
```

source 配置（标的、周期、序列等）仍在 [`configs/collection/sources/`](configs/collection/sources/)，不含密钥。

## 查询

```python
from alpha.collection import load_ohlcv, load_funding, load_fundamental, load_macro, load_trades, load_quote

rows = load_ohlcv("data", venue="binance", tf="1h")
stocks = load_ohlcv("data", venue="alpaca", tf="1d")
trades = load_trades("data", venue="alpaca", symbol_raw="AAPL")
quotes = load_quote("data", venue="alpaca", symbol_raw="AAPL")
funding = load_funding("data", venue="binance")
fundamentals = load_fundamental("data", venue="finnhub", symbol_raw="AAPL")
gdp = load_macro("data", venue="fred", symbol_raw="GDP")
```

- [`data/canonical/`](data/canonical/)：归一化 Parquet，给研究用  
- [`data/raw/`](data/raw/)：接口原始 JSONL，给排障/对照字段用  
- 用 DBX / DuckDB 查看的配置说明见 [`ARCHITECTURE.md`](ARCHITECTURE.md)「如何查看数据」

## 研究、回测与执行

研究配置放在 [`configs/research/`](configs/research/)。特征可读取 OHLCV、funding、fundamental、macro、trade、quote 等任意组合的 canonical 数据集；首个示例策略仍以 OHLCV 为锚点。bar 级回测的信号在 bar 收盘形成，并在下一根 bar 的开盘价模拟成交。

```bash
alpha feature run --research-config configs/research/moving_average_cross.yaml
alpha backtest run --research-config configs/research/moving_average_cross.yaml
```

特征写入 `data/features/{feature_set}/features.parquet`；回测写入 `data/backtests/{run_id}/`，包括 `summary.json`、净值/权重时间序列和 `orders.parquet` 虚拟订单明细。真实交易执行属于独立的 `execution` 模块，当前仅提供纸面执行与风控边界。

研究 YAML 可用 `inputs` 声明多个数据集；各特征会声明所需数据集，特征集以 `anchor_dataset` 的时间与标的坐标输出。例如：

```yaml
inputs:
  ohlcv:
    venue: binance
    market_type: spot
    tf: 1h
  funding:
    venue: binance
    market_type: perp
  macro:
    venue: fred
    symbol_raw: CPIAUCSL
```

## 文档

- 表结构与字段解释：[`SCHEMA.md`](SCHEMA.md)
- 架构、目录含义、分期与各源 quirks：[`ARCHITECTURE.md`](ARCHITECTURE.md)
