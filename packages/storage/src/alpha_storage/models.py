"""ORM 模型。字段定义与 research/SCHEMA.md 保持一致，改动需要同一改动内同步文档。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CHAR, TIMESTAMP, BigInteger, Integer, Numeric, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PoolCandidateRow(Base):
    """候选池目录：Factory `PoolCreated` 全量扫描的落库结果。

    这是 pool-discovery-metrics-v1.md 第 0 节所说"重新引入全量目录"的落地——
    与 alpha-lp 的 `pools` 表是两回事：那张表只存"已确认关联到某个仓位/钱包"的池子，
    这张表存"Factory 里存在过的全部池子"，服务于池子发现打分场景，两者不合并。
    """

    __tablename__ = "pool_candidates"
    __table_args__ = (UniqueConstraint("chain", "pool_address", name="uq_pool_candidates_chain_pool"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(CHAR(10))  # 链标识，取值见 alpha_core.types.Chain
    dex_id: Mapped[str] = mapped_column(CHAR(30))  # DEX 标识，取值见 alpha_core.types.DexId
    pool_address: Mapped[str] = mapped_column(CHAR(42))  # 池子合约地址，小写
    token0_address: Mapped[str] = mapped_column(CHAR(42))
    token1_address: Mapped[str] = mapped_column(CHAR(42))
    fee_pips: Mapped[int]  # 手续费费率，单位 1e-6
    tick_spacing: Mapped[int]
    created_at_block: Mapped[int]  # PoolCreated 事件所在区块号
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))  # 懒加载补齐，见 7.4
    status: Mapped[str] = mapped_column(CHAR(10))  # discovered/qualified/rejected，见 PoolCandidateStatus
    asset_class: Mapped[str] = mapped_column(
        CHAR(15), server_default="crypto_native"
    )  # crypto_native/rwa，见 alpha_core.types.AssetClass；决定 RWA 专属特征/模型要不要跑
    discovered_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))  # 本系统首次发现该池子的时间
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))


class PoolMetricsHistoryRow(Base):
    """候选池逐日历史快照，波动率等指标计算的时间序列基础（1b 阶段消费）。"""

    __tablename__ = "pool_metrics_history"
    __table_args__ = (
        UniqueConstraint(
            "chain", "pool_address", "snapshot_date", name="uq_pool_metrics_history_pool_date"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(CHAR(10))
    pool_address: Mapped[str] = mapped_column(CHAR(42))
    snapshot_date: Mapped[date]  # 快照所属自然日（UTC）
    tvl_usd: Mapped[float | None]  # 当日 TVL；None 表示当天未取到（不能当 0 处理）
    volume_24h_usd: Mapped[float | None]  # 截至当日的滚动 24h 交易量
    close_price: Mapped[float | None]  # 当日收盘价（token0/token1 汇率）
    data_source: Mapped[str] = mapped_column(CHAR(20))  # 数据来源标识，如 geckoterminal
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))  # 实际抓取时间


class ChainCursorRow(Base):
    """按（链, DEX）维护的 Factory 扫描水位线，支持增量扫描而不是每次全量重扫。"""

    __tablename__ = "chain_cursors"
    __table_args__ = (UniqueConstraint("chain", "dex_id", name="uq_chain_cursors_chain_dex"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(CHAR(10))
    dex_id: Mapped[str] = mapped_column(CHAR(30))
    last_scanned_block: Mapped[int]  # 已扫描到的最后一个（已终结）区块号
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))


class InstrumentRow(Base):
    """标的目录：跨资产类型（链上池子/未来 CEX/未来股票）统一的标的登记表。

    字段命名对齐 `alpha-research-engine`（一个专门的离线行情研究平台）的 `instruments` 目录约定，
    `instrument_id` 用 `{venue}:{market_type}:{symbol_raw}` 这个字符串本身做主键，不加自增 id——
    这是跨系统都会直接引用的自然键，见 research/docs/live-signal-system-设计方案.md 的
    "对齐 alpha-research-engine 命名约定"一节。本期只登记链上 DEX 池子，`chain` 字段是本表相对
    对方 catalog 的唯一新增字段（对方管的都是链下市场，没有"链"这个概念）。
    """

    __tablename__ = "instruments"

    # TEXT 而非 CHAR(N)：instrument_id 是变长自然键，会被读回 Python 后做原始字符串比较/JSON
    # 序列化（如 /conclusion 接口透传、聚合脚本按 instrument_id 分组）——CHAR(N) 读回来会带
    # 尾随空格 padding（已用真实查询验证：SQLAlchemy/psycopg 不会自动 strip），SQL 层
    # `WHERE instrument_id = ...` 能正常工作（Postgres 按 SQL 标准语义忽略尾随空格），但脱离
    # SQL 上下文的原始 Python 比较/输出会静默出错。地址类字段用 CHAR(42) 没有这个问题，因为
    # 内容长度正好等于声明长度，从不会被 padding。
    instrument_id: Mapped[str] = mapped_column(Text, primary_key=True)  # "{venue}:{market_type}:{symbol_raw}"
    venue: Mapped[str] = mapped_column(CHAR(30))  # 如 pancakeswap-v3-bsc，本期取值对齐 alpha_core.types.DexId
    market_type: Mapped[str] = mapped_column(CHAR(20))  # 本期只有 dex_pool；开放字符串，以后加新值不改表结构
    base: Mapped[str] = mapped_column(CHAR(42))  # base token 地址（链上场景：字典序较小的一侧）
    quote: Mapped[str] = mapped_column(CHAR(42))  # quote token 地址
    settle: Mapped[str | None] = mapped_column(CHAR(42))  # 结算资产；链上 AMM 没有独立结算概念，恒为 NULL
    symbol_raw: Mapped[str] = mapped_column(CHAR(42))  # 链上场景下就是池子地址
    chain: Mapped[str] = mapped_column(CHAR(10))  # 链标识，见 alpha_core.types.Chain
    listed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))  # 暂不回填
    delisted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))  # 暂不使用
    meta_json: Mapped[dict | None] = mapped_column(JSONB)  # 预留（fee_pips/tick_spacing 等），本期不填


class SwapEventRow(Base):
    """PancakeSwap V3 池子的逐笔 `Swap` 事件，WebSocket 订阅落库的最细粒度原始数据。

    只服务链上场景，不需要跨资产类型通用化——CEX/股票没有"链上 swap 事件"这个概念，
    以后各自会有自己的逐笔成交表，不共用这张表。
    """

    __tablename__ = "swap_events"
    __table_args__ = (
        UniqueConstraint("chain", "tx_hash", "log_index", name="uq_swap_events_chain_tx_log"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[str] = mapped_column(CHAR(10))
    pool_address: Mapped[str] = mapped_column(CHAR(42))
    instrument_id: Mapped[str] = mapped_column(Text)  # 逻辑外键 -> instruments.instrument_id，不加约束
    # ↑ TEXT 而非 CHAR(N)：理由见 InstrumentRow.instrument_id 的注释
    tx_hash: Mapped[str] = mapped_column(CHAR(66))
    log_index: Mapped[int] = mapped_column(Integer)
    block_number: Mapped[int] = mapped_column(BigInteger)
    sender: Mapped[str] = mapped_column(CHAR(42))  # 通常是 Router 合约地址
    recipient: Mapped[str] = mapped_column(CHAR(42))
    amount0: Mapped[int] = mapped_column(Numeric(78, 0))  # token0 变动量，带符号（池子视角：正=流入）
    amount1: Mapped[int] = mapped_column(Numeric(78, 0))  # token1 变动量，带符号
    sqrt_price_x96_after: Mapped[int] = mapped_column(Numeric(78, 0))  # 成交后 √价格（Q64.96 定点数）
    tick_after: Mapped[int] = mapped_column(Integer)  # 成交后所在 tick
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))  # 实际捕获时间
    block_time: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))  # 出块时间，K 线聚合分桶用这个
    # WebSocket 订阅路径从推送 payload 免费拿到；HTTP 回填路径额外调 get_block_timestamp 补齐，
    # 见 alpha_chains.evm_websocket 的模块文档——加这一列是为了让 aggregate_candles 不用每次
    # 重新查区块时间戳（那样跑几天后会越来越慢，见实施记录）。可空是因为这一列是后加的，
    # 加列之前落库的历史行没有这个值，需要单独脚本回填；新写入的行（SwapEvent 模型要求必填）
    # 恒有值


class OhlcvRow(Base):
    """链上池子的 K 线。字段命名对齐 `alpha-research-engine` 的 envelope + ohlcv 专属字段约定
    （`ts_event`/`tf`/`volume`/`quote_volume`/`trade_count`），物理存储仍是 Postgres
    （不是对方用的 Parquet）——WebSocket 实时写入 + HTTP 接口查最新一分钟这个访问模式，
    Postgres 更合适，字段/命名对齐是为了以后互通/归档时不用重新设计表结构，
    见 research/docs/live-signal-system-设计方案.md。

    表名是 `pool_ohlcv`（不是 `ohlcv`）——这张表只存链上池子的 K 线，以后要接 CEX/股票的
    OHLCV 会是各自独立的表（如 `cex_ohlcv`/`stock_ohlcv`），不会混进这张表，表名直接体现
    这个边界，不用等到真的接入第二种资产类型才发现名字取早了。`dataset` 字段恒为 `'ohlcv'`，
    描述的是"这行数据是什么类型"（信封约定），跟表名是两个不同维度，不需要跟着表名改。
    """

    __tablename__ = "pool_ohlcv"
    __table_args__ = (
        UniqueConstraint("instrument_id", "tf", "ts_event", name="uq_pool_ohlcv_instrument_tf_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dataset: Mapped[str] = mapped_column(CHAR(10), server_default="ohlcv")
    instrument_id: Mapped[str] = mapped_column(Text)  # 逻辑外键 -> instruments.instrument_id，不加约束
    # ↑ TEXT 而非 CHAR(N)：理由见 InstrumentRow.instrument_id 的注释
    venue: Mapped[str] = mapped_column(CHAR(30))  # 冗余存一份，避免每次查询都要 join instruments
    tf: Mapped[str] = mapped_column(CHAR(4))  # K 线粒度，本期只有 '1m'
    ts_event: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))  # K 线开盘时间，不是收盘时间
    ts_ingest: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))  # 聚合脚本写入时间
    open: Mapped[float] = mapped_column(Numeric)  # token0/token1 汇率，不是 USD（1c 阶段的教训）
    high: Mapped[float] = mapped_column(Numeric)
    low: Mapped[float] = mapped_column(Numeric)
    close: Mapped[float] = mapped_column(Numeric)
    volume: Mapped[float] = mapped_column(Numeric)  # base（token0）成交量绝对值之和
    quote_volume: Mapped[float | None] = mapped_column(Numeric)  # quote（token1）成交量绝对值之和
    trade_count: Mapped[int | None] = mapped_column(Integer)  # 该分钟内成交笔数


class TokenRow(Base):
    """ERC20 token 的链上元数据缓存。`decimals` 是标准里的不可变值，一经查到就永久有效——
    之前 `aggregate_candles` 每次调用都现场 `eth_call` 重新查一遍（20 CU/次，真实跑过才发现
    这是纯浪费，见实施记录），这张表让"同一个 token 只查一次链"落到实处，不是进程内存缓存
    （那样每次新起 CLI 进程缓存都清零，等于没缓存）。

    `alpha_core.models.Token` 这个领域对象在 1a 阶段就写好了、专门标注"decimals/symbol
    应当永久缓存"，但当时没有配套的存储层——这张表补上这个缺口，不是新发明的概念。
    """

    __tablename__ = "tokens"

    chain: Mapped[str] = mapped_column(CHAR(10), primary_key=True)
    token_address: Mapped[str] = mapped_column(CHAR(42), primary_key=True)  # 统一小写存储
    decimals: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))  # 首次查到并落库的时间
