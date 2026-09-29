"""跨模块共用的枚举与值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Chain(StrEnum):
    """research 已支持的链。取值与 alpha-lp `SupportedChain.key` 保持一致（alpha-lp 没有的链自行命名），
    便于跨语言对照。新增一条链：在这里加成员，并在 `CHAIN_SPECS` 里登记规格。
    """

    BSC = "bsc"  # BNB Chain，chainId 56；lp-backtest、live-signal 只用这条链
    ETHEREUM = "ethereum"  # 以太坊主网，chainId 1；alpha-lp 暂无这条链
    BASE = "base"  # Base（OP Stack L2），chainId 8453


@dataclass(frozen=True)
class ChainSpec:
    """链适配器需要的链规格。只放构造 RPC 适配器用得到的信息；gas 模型、包装原生币等解码用的差异
    放在 `alpha_protocols` 的链画像里（`alpha_chains` 不能依赖 `alpha_protocols`）。
    """

    chain: Chain
    chain_id: int  # EIP-155 chainId
    # 环境变量前缀：`<前缀>_RPC_URLS`、`<前缀>_LOG_CHUNK_SIZE`、`<前缀>_RPC_MAX_CUPS`、`<前缀>_RPC_BATCH_SIZE`
    env_prefix: str
    is_poa: bool  # 区块头 extraData 超过 32 字节（PoA/Clique 类共识），web3 需要注入 POA 中间件
    default_log_chunk_size: int | None  # 单次 eth_getLogs 的默认区块跨度；None 表示用适配器的保守默认值


# 已登记的链规格。BSC 的环境变量前缀沿用 alpha-lp 的 `BNB_`，两边共用同一套 RPC 密钥。
CHAIN_SPECS: dict[Chain, ChainSpec] = {
    # 45000：alpha-lp 生产 RPC 实测单次最多 50000 个区块，取略低的保守值（见 alpha_chains.bsc）
    Chain.BSC: ChainSpec(Chain.BSC, 56, "BNB", is_poa=True, default_log_chunk_size=45_000),
    Chain.ETHEREUM: ChainSpec(Chain.ETHEREUM, 1, "ETH", is_poa=False, default_log_chunk_size=None),
    Chain.BASE: ChainSpec(Chain.BASE, 8453, "BASE", is_poa=False, default_log_chunk_size=None),
}

# 各链的 EVM chainId，外部接口（Sourcify 等）按 chainId 区分链时使用。
EVM_CHAIN_IDS: dict[Chain, int] = {c: spec.chain_id for c, spec in CHAIN_SPECS.items()}


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
