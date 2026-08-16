"""领域对象：Chain / Token / PoolCandidate / PoolMetricsSnapshot 等。

本期（lp-backtest 1a）范围：候选池发现 + 历史数据接入。
指标计算（FeeAPR / σ_price / CompositeScore 等，1b 阶段）在此基础上派生，不在这里定义。
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from alpha_core.types import Chain, DexId, PoolCandidateStatus


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

    @field_validator("pool_address", "token0_address", "token1_address")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        return _normalize_address(v)


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
