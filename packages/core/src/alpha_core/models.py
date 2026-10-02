"""领域对象：Chain / Token / PoolCandidate / PoolMetricsSnapshot 等。

本期（lp-backtest 1a）范围：候选池发现 + 历史数据接入。
指标计算（FeeAPR / σ_price / CompositeScore 等，1b 阶段）在此基础上派生，不在这里定义。
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from alpha_core.instrument_id import build_instrument_id
from alpha_core.types import AssetClass, Chain, DexId, PoolCandidateStatus


def _normalize_address(value: str) -> str:
    """链上地址统一小写存储，与 alpha-lp SCHEMA.md 的地址约定（lowercase CHAR(42)）保持一致。"""
    if not value.startswith("0x") or len(value) != 42:
        raise ValueError(f"不是合法的 EVM 地址: {value!r}")
    return value.lower()


class Token(BaseModel):
    """ERC20 token 元数据。decimals/symbol 链上不可变，抓到之后应当永久缓存，不重复请求。"""

    model_config = {"frozen": True}

    chain: Chain
    address: str  # 合约地址，统一小写存储
    symbol: str | None = None  # 代币符号，未解析出时为 None
    decimals: int | None = None  # 精度位数，未解析出时为 None（不能默认按 18 处理，避免金额算错）

    @field_validator("address")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        return _normalize_address(v)


class PoolCandidate(BaseModel):
    """从 Factory `PoolCreated` 事件发现的候选池。

    这是"全量发现"的产出——身份权威来自链上 Factory 事件，
    GeckoTerminal 等第三方数据只作为后续收益/风险信号来源，不作为池子存在性权威
    （对应 pool-discovery-metrics-v1.md 第 0 节）。
    """

    model_config = {"frozen": True}

    chain: Chain
    dex_id: DexId
    pool_address: str  # 池子合约地址，统一小写存储
    token0_address: str  # Factory 事件里的 token0（地址字典序较小的一侧），统一小写存储
    token1_address: str  # Factory 事件里的 token1
    fee_pips: int = Field(ge=0, le=1_000_000)  # 池子手续费费率，单位 1e-6（如 2500 = 0.25%）
    tick_spacing: int  # 池子 tick 间距，由 fee_pips 在 Factory 里唯一决定，一并落库避免重复推导
    created_at_block: int = Field(ge=0)  # PoolCreated 事件所在区块号
    created_at: datetime | None = None  # 区块时间戳；按需懒加载获得，未回填前为 None（见 7.4 懒加载约束）
    status: PoolCandidateStatus = PoolCandidateStatus.DISCOVERED
    asset_class: AssetClass = AssetClass.CRYPTO_NATIVE  # 资产类型，决定 RWA 专属特征/模型要不要跑

    @field_validator("pool_address", "token0_address", "token1_address")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        return _normalize_address(v)


class Instrument(BaseModel):
    """`instruments` 目录表的领域表示。字段命名对齐 `alpha-research-engine` 的 envelope 约定，
    见 research/docs/live-signal-system-设计方案.md。本期只覆盖链上 DEX 池子这一种 `market_type`。
    """

    model_config = {"frozen": True}

    venue: str  # 如 "pancakeswap-v3-bsc"，本期取值对齐 DexId
    market_type: str  # 本期只有 "dex_pool"，开放字符串
    base: str  # base token 地址，统一小写存储
    quote: str  # quote token 地址
    settle: str | None = None  # 链上 AMM 没有独立结算资产概念，恒为 None
    symbol_raw: str  # 链上场景下就是池子地址
    chain: Chain
    listed_at: datetime | None = None
    delisted_at: datetime | None = None
    meta_json: dict | None = None

    @field_validator("base", "quote", "symbol_raw")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        return _normalize_address(v)

    @property
    def instrument_id(self) -> str:
        return build_instrument_id(venue=self.venue, market_type=self.market_type, symbol_raw=self.symbol_raw)


class SwapEvent(BaseModel):
    """`swap_events` 的领域表示：PancakeSwap V3 池子的逐笔 `Swap` 事件。

    只服务链上场景（`instrument_id` 指向对应的 `Instrument`），字段对应
    `alpha_storage.models.SwapEventRow`，见 research/SCHEMA.md 第 7 节。
    """

    model_config = {"frozen": True}

    chain: Chain
    pool_address: str  # 统一小写存储
    instrument_id: str  # 逻辑外键 -> instruments.instrument_id
    tx_hash: str
    log_index: int
    block_number: int
    sender: str  # 统一小写存储
    recipient: str
    amount0: int  # token0 变动量，带符号（池子视角：正=流入）
    amount1: int  # token1 变动量，带符号
    sqrt_price_x96_after: int  # 成交后 √价格（Q64.96 定点数）
    tick_after: int  # 成交后所在 tick
    fetched_at: datetime  # 实际捕获时间（进程收到/回填时的时间，不是这笔交易真实发生的时间）
    block_time: datetime  # 出块时间（K 线聚合分桶用这个，不用 fetched_at）；WebSocket 订阅路径
    # 从推送 payload 免费拿到，HTTP 回填路径要额外调 ChainAdapter.get_block_timestamp 补齐，
    # 见 alpha_chains.evm_websocket 的模块文档

    @field_validator("pool_address", "sender", "recipient")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        return _normalize_address(v)


class OhlcvCandle(BaseModel):
    """`pool_ohlcv` 的领域表示：一根 K 线。字段对应 `alpha_storage.models.OhlcvRow`，见
    research/SCHEMA.md 第 8 节。`open`/`high`/`low`/`close` 是 token1/token0 汇率
    （不是 USD——1c 阶段因为单位不统一吃过真实 bug，这次直接按对的口径设计）。
    """

    model_config = {"frozen": True}

    instrument_id: str
    venue: str
    tf: str  # K 线粒度，本期只有 "1m"
    ts_event: datetime  # K 线开盘时间（真实区块时间，不是抓取时间）
    ts_ingest: datetime  # 聚合脚本写入时间
    open: float
    high: float
    low: float
    close: float
    volume: float  # base（token0）成交量绝对值之和，已按 decimals 换算
    quote_volume: float | None = None  # quote（token1）成交量绝对值之和
    trade_count: int | None = None  # 该分钟内成交笔数


class PoolMetricsSnapshot(BaseModel):
    """`pool_metrics_history` 的领域表示：候选池的逐日历史快照。

    本期只承载"原始数据"（TVL/24h volume/收盘价），
    FeeAPR / σ_price / CompositeScore 等派生指标由 1b 阶段的 features 层在此基础上计算，不在快照里冗余存储。
    """

    model_config = {"frozen": True}

    chain: Chain
    pool_address: str  # 统一小写存储
    snapshot_date: date  # 快照所属自然日（UTC），同一池子同一天只保留一条
    tvl_usd: float | None = None  # 池子当日 TVL（USD），None 表示当天未能从数据源取到
    volume_24h_usd: float | None = None  # 截至当日的滚动 24h 交易量（USD）
    close_price: float | None = None  # 当日收盘价（token0/token1 汇率，口径与 OHLCV 数据源一致）
    data_source: str = "geckoterminal"  # 数据来源标识，便于后续排查口径问题或切换数据源
    fetched_at: datetime  # 实际抓取时间（非 snapshot_date），用于判断数据新鲜度

    @field_validator("pool_address")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        return _normalize_address(v)
