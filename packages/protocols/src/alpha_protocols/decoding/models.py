"""解码层的数据模型：资产流水、标准事件、持仓引用、解码结果。

这一层与链无关：地址、资产、交易哈希都当作不透明字符串处理，不假设它们是 EVM 格式；
与链有关的提取逻辑在 `decoding/evm/`，链与链之间的差异由链画像声明（规划 5.12）。

所有模型都是不可变的值对象。解码是纯函数，同一输入重复解码必须得到逐字段相同的结果，
M3 的人工修正按 `(chain, tx_hash, seq, subject_wallet)` 定位事件，依赖这一点。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# 原生币（BNB、ETH……）在 `asset` 字段里的取值。各链原生币的符号和精度在链画像里。
NATIVE = "native"


class AssetFlowKind(StrEnum):
    """资产流水的类型。"""

    NATIVE = "native"  # 交易本身携带的原生币（tx.value）
    INTERNAL = "internal"  # 数据源给出的内部调用原生币转移
    INFERRED_NATIVE = "inferred_native"  # 家族依据自己的事件确定性推断出的原生币转移（规划 5.5）
    SYSTEM_MINT = "system_mint"  # 链的系统交易凭空铸造的原生币（OP Stack 存款交易的 mint），由链画像的系统交易规则产生
    ERC20 = "erc20"  # 可替代 token 转账
    ERC721 = "erc721"  # 不可替代 token（NFT）转账，数量恒为 1
    ERC1155 = "erc1155"  # 半同质化 token 转账
    GAS = "gas"  # 交易发起人支付的 gas 费（含 L2 的 L1 数据费）


class FlowSource(StrEnum):
    """资产流水来自哪里。"""

    TX = "tx"  # 交易字段（value、gas、系统交易的 mint）
    LOG = "log"  # 回执日志
    INTERNAL = "internal"  # 数据源给出的内部交易
    INFERRED = "inferred"  # 推断钩子


@dataclass(frozen=True)
class AssetFlow:
    """第一段通用解码产出的一条资产流水，后续所有语义都建立在它之上。

    流水不带方向：方向是相对某个钱包而言的，在事件里才确定。
    """

    flow_id: int  # 交易内序号，从 0 开始；按（log_index，交易级流水排在最前）排序后编号，同一输入稳定
    kind: AssetFlowKind
    asset: str  # token 合约地址；原生币为 NATIVE
    amount_raw: int  # 原始整数数量，≥0，不做精度换算（设计文档 G3）；NFT 为 1
    from_address: str
    to_address: str
    source: FlowSource
    token_id: int | None = None  # NFT 和 ERC1155 的 tokenId；其他为 None
    log_index: int | None = None  # 来自日志时为日志序号；来自交易字段或内部交易时为 None

    def __post_init__(self) -> None:
        if self.amount_raw < 0:
            raise ValueError(f"资产流水数量不能为负：{self.amount_raw}")


class Direction(StrEnum):
    """事件相对主体钱包的方向。"""

    IN = "in"  # 资产流入钱包
    OUT = "out"  # 资产流出钱包
    NEUTRAL = "neutral"  # 不改变钱包资产（授权、状态事件、包装凭证互换中的中性记录等）


class EventType(StrEnum):
    """标准事件的大类。合法的 (类型, 子类型, 方向) 组合见 `taxonomy`。"""

    TRADE = "trade"  # 交换：付出一种资产、得到另一种
    DEPOSIT = "deposit"  # 资产存入协议
    WITHDRAWAL = "withdrawal"  # 从协议取回资产
    BORROW = "borrow"  # 借入，负债增加
    REPAY = "repay"  # 还款或被清算，负债减少
    STAKE = "stake"  # 质押
    UNSTAKE = "unstake"  # 解押
    CLAIM = "claim"  # 领取奖励、手续费、利息
    TRANSFER = "transfer"  # 未被任何家族认领的普通转账
    SPEND = "spend"  # 交出凭证或被强制转走资产
    RECEIVE = "receive"  # 拿到凭证，或不请自来的转入
    BRIDGE = "bridge"  # 跨链转出或转入
    MINT = "mint"  # 仓位 NFT 铸造
    BURN = "burn"  # 仓位 NFT 销毁
    FEE = "fee"  # gas 费或协议另收的费用
    INFORMATIONAL = "informational"  # 不动资产的交互（授权、签到、未知合约的可读事件）


class EventSubtype(StrEnum):
    """标准事件的细分。"""

    NONE = "none"  # 无细分
    DEPOSIT_ASSET = "deposit_asset"  # 存入的是资产本身
    REMOVE_ASSET = "remove_asset"  # 取回的是资产本身
    RECEIVE_WRAPPED = "receive_wrapped"  # 拿到协议凭证（WBNB、LP、vToken、金库份额）
    RETURN_WRAPPED = "return_wrapped"  # 交回协议凭证
    SPEND = "spend"  # 交换的付出腿
    RECEIVE = "receive"  # 交换的收到腿
    GENERATE_DEBT = "generate_debt"  # 产生负债
    PAYBACK_DEBT = "payback_debt"  # 偿还负债
    LIQUIDATE = "liquidate"  # 清算相关
    REWARD = "reward"  # 协议奖励（XVS、CAKE……）
    LP_FEE = "lp_fee"  # LP 手续费
    INTEREST = "interest"  # 利息（协议单独发放时）
    AIRDROP = "airdrop"  # 不请自来的普通转入
    SPAM = "spam"  # 风险 token 的转入或转出（仿冒、垃圾空投、地址投毒）
    PROTOCOL_FEE = "protocol_fee"  # 协议另收的费用
    APPROVE = "approve"  # 授权
    DECODED_LOG = "decoded_log"  # 未知合约的日志，用 ABI 解出了事件名和参数


class CoverageTier(StrEnum):
    """事件的覆盖等级，只升不降（设计文档 3.3）。"""

    T0 = "T0"  # 通用：资产流动正确，不知道是什么协议
    T1 = "T1"  # 已识别：发出合约有识别结果
    T2 = "T2"  # 已解码：有协议语义和持仓键（交换事件也算）
    T3 = "T3"  # 可估值：对应的持仓能按家族估值


class Confidence(StrEnum):
    """事件是否依赖推断。"""

    EXACT = "exact"  # 全部来自链上数据
    INFERRED = "inferred"  # 依赖确定性推断（例如由事件推出的原生币流动）


class PositionKind(StrEnum):
    """持仓形态（规划 5.6）。"""

    SHARE = "share"  # 可替代份额：持有凭证 token，可按规则换回底层资产（V2 LP、vToken、金库份额）
    NFT = "nft"  # NFT 仓位：每个仓位参数不同（V3）
    DEBT = "debt"  # 负债：借入的资产，随利息增长，估值为负
    STAKED = "staked"  # 质押或锁仓：资产在合约里，归属于钱包
    CLAIMABLE = "claimable"  # 待领奖励：尚未到账的奖励和手续费


@dataclass(frozen=True)
class PositionRef:
    """一个持仓的引用，也是估值的输入。同一个协议在不同链上的持仓是不同的持仓。"""

    chain: str  # 链，例如 "bsc"
    instance_key: str  # 实例键，例如 "venus-core"
    kind: PositionKind
    id: str  # 形态内的标识：share 是凭证 token 地址，nft 是 tokenId，debt 是市场地址
    owner: str  # 持有人

    @property
    def key(self) -> str:
        """持仓键 `<chain>:<instance_key>:<kind>:<id>`，写进事件的 `position_key`。"""
        return f"{self.chain}:{self.instance_key}:{self.kind.value}:{self.id}"


@dataclass(frozen=True)
class NormalizedEvent:
    """某个钱包视角下的一条标准事件（设计文档 3.3 的标准事件表）。"""

    chain: str
    tx_hash: str
    seq: int  # 交易内序号，同一输入稳定
    subject_wallet: str  # 从哪个钱包的视角
    event_type: EventType
    event_subtype: EventSubtype
    direction: Direction
    decoder_version: str  # `<family>@<整数版本>`；兜底部分为 `generic@<版本>`
    coverage_tier: CoverageTier
    asset: str | None = None  # 资产；原生币为 NATIVE；纯状态且不涉及资产的事件为 None
    amount_raw: int | None = None  # 流动事件为被认领流水的合计；状态事件为家族给出的数量（例如负债减少额）
    token_id: int | None = None  # NFT 的 tokenId
    claimed_flow_ids: tuple[int, ...] = ()  # 认领的流水；为空表示没有资产流动的状态事件
    counterparty_address: str | None = None  # 对手方地址
    family: str | None = None  # 协议家族键；未识别为 None
    instance_key: str | None = None  # 协议实例键（设计文档中的 protocol）；未识别或未命名分叉为 None
    position_key: str | None = None  # 持仓键，见 `PositionRef.key`
    confidence: Confidence = Confidence.EXACT
    extra: Mapping[str, Any] = field(default_factory=dict)  # 家族特有字段，例如 tick 区间、健康度


class WarningCode(StrEnum):
    """解码告警。告警不影响已产出事件的正确性，但说明结果可能不完整。"""

    INTERNAL_UNAVAILABLE = "internal_unavailable"  # 数据源没给内部交易，而这笔交易很可能有原生币内部转移
    ABI_MISSING = "abi_missing"  # 未知合约的日志找不到 ABI，只能保留资产流动
    UNKNOWN_CONTRACT = "unknown_contract"  # 发出日志的合约没有识别结果


@dataclass(frozen=True)
class DecodeWarning:
    code: WarningCode
    detail: str  # 人读的说明，例如涉及的合约地址或日志序号


@dataclass(frozen=True)
class DecodedTx:
    """一笔交易在某个钱包视角下的解码结果。"""

    chain: str
    tx_hash: str
    subject_wallet: str
    succeeded: bool  # 交易是否执行成功；失败的交易只有 gas 事件
    events: tuple[NormalizedEvent, ...]
    flows: tuple[AssetFlow, ...]  # 第一段产出的全部流水，供追溯和对账
    unclaimed_flow_ids: tuple[int, ...] = ()  # 兜底之后仍未被任何事件认领的流水；正常情况下为空
    unknown_contracts: tuple[str, ...] = ()  # 发出日志但没有识别结果的合约
    warnings: tuple[DecodeWarning, ...] = ()
