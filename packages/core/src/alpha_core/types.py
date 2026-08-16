"""跨模块共用的枚举与值对象。"""

from __future__ import annotations

from enum import StrEnum


class Chain(StrEnum):
    """research 已支持的链。取值与 alpha-lp `SupportedChain.key` 保持一致，便于跨语言对照。"""

    BSC = "bsc"  # BNB Chain，chainId 56，本期（lp-backtest 1a）唯一支持的链


class DexId(StrEnum):
    """已知的 DEX 标识。取值对齐 GeckoTerminal 的 dex id，避免自造一套映射。"""

    PANCAKESWAP_V3_BSC = "pancakeswap-v3-bsc"  # PancakeSwap V3（BSC），本期唯一支持的协议


class MetricAvailability(StrEnum):
    """指标可得性三态，沿用 alpha-lp `AprEstimateView` 的约定：
    不能把"数据不可得"静默按 0 处理，必须显式区分"确认为 0"和"算不出来"。
    """

    AVAILABLE = "available"  # 指标已按公式算出，值本身可信
    NO_INCENTIVE = "no_incentive"  # 特指激励类指标：确认当前无激励（如 CAKE emission 为 0），不是缺数据
    UNAVAILABLE = "unavailable"  # 数据不足以计算（如波动率回看窗口不足 30 天），不应参与打分/归一化


class DepthTier(StrEnum):
    """TVL 深度分档，供用户判断"这个 APR 我能吃到多少"。阈值见 pool-discovery-metrics-v1.md 1.3 节。"""

    HIGH_RISK = "high_risk"  # TVL < $100k：浅池，滑点/退出冲击风险高
    MEDIUM = "medium"  # $100k <= TVL < $1M
    LOW_RISK = "low_risk"  # TVL >= $1M


class PoolCandidateStatus(StrEnum):
    """候选池生命周期状态。"""

    DISCOVERED = "discovered"  # 已通过 Factory PoolCreated 事件发现，尚未判定是否达到准入门槛
    QUALIFIED = "qualified"  # 已过硬性准入门槛（TVL/volume/池龄/token 白名单），参与后续指标计算与打分
    REJECTED = "rejected"  # 未达门槛，不产生分数、不纳入 pool_metrics_history 采集范围


class AssetClass(StrEnum):
    """候选池的资产类型，决定 RWA 专属特征/模型（参考价、TrackingError、GapJump）要不要跑。
    见 research/docs/live-signal-system-设计方案.md 第 3.6 节及 asset_class 相关设计。
    """

    CRYPTO_NATIVE = "crypto_native"  # 加密原生资产对（如 BTC/USDT），不需要任何 RWA 专属模块
    RWA = "rwa"  # 锚定真实世界资产的代币化凭证（如 QQQB），需要参考价数据源等专属处理
