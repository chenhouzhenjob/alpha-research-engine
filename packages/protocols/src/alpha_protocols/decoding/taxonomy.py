"""事件分类表：合法的 (类型, 子类型) 组合、允许的方向，以及是否必须认领资产流水。

表一次写全，覆盖第二阶段才实现的质押、跨链等（规划 5.3）。存入协议统一写成"资产 ↔ 凭证"的对称形态：
存入一侧 `deposit/deposit_asset`，拿到凭证一侧 `receive/receive_wrapped`；取回反过来。WBNB、V2 LP、
vToken、金库份额都按这个形态，M3 记账不用为每个家族写特例。

任何家族产出不在表里的组合，`validate_event` 都会抛 `TaxonomyError`；测试遍历全部家族的输出做这个检查。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import Direction, EventSubtype, EventType, NormalizedEvent

IN, OUT, NEUTRAL = Direction.IN, Direction.OUT, Direction.NEUTRAL


class FlowRule(StrEnum):
    """一种组合对资产流水的要求。"""

    REQUIRED = "required"  # 必须认领至少一条资产流水
    FORBIDDEN = "forbidden"  # 不能认领资产流水（纯状态或纯信息事件）
    OPTIONAL = "optional"  # 两种都可以（例如还款：还款人视角有流水，被代还的借款人视角没有）


@dataclass(frozen=True)
class Category:
    """一种合法组合的定义。"""

    directions: frozenset[Direction]  # 允许的方向
    flows: FlowRule
    meaning: str  # 中文语义，供文档和 AI 工具说明使用


def _c(directions: set[Direction], flows: FlowRule, meaning: str) -> Category:
    return Category(frozenset(directions), flows, meaning)


T, S = EventType, EventSubtype
REQ, NO, OPT = FlowRule.REQUIRED, FlowRule.FORBIDDEN, FlowRule.OPTIONAL

CATEGORIES: dict[tuple[EventType, EventSubtype], Category] = {
    (T.TRANSFER, S.NONE): _c({IN, OUT}, REQ, "未被任何家族认领的普通转账"),
    (T.RECEIVE, S.AIRDROP): _c({IN}, REQ, "不请自来的普通转入"),
    (T.RECEIVE, S.SPAM): _c({IN, OUT}, REQ, "风险 token 的转入，或地址投毒伪造的转出记录"),
    (T.RECEIVE, S.RECEIVE_WRAPPED): _c({IN}, REQ, "拿到协议凭证（WBNB、LP、vToken、金库份额）"),
    (T.SPEND, S.RETURN_WRAPPED): _c({OUT}, REQ, "交回协议凭证"),
    (T.DEPOSIT, S.DEPOSIT_ASSET): _c({OUT}, REQ, "资产存入协议（含包装原生币、添加流动性、存款）"),
    (T.WITHDRAWAL, S.REMOVE_ASSET): _c({IN}, REQ, "从协议取回资产"),
    (T.TRADE, S.SPEND): _c({OUT}, REQ, "交换的付出腿"),
    (T.TRADE, S.RECEIVE): _c({IN}, REQ, "交换的收到腿"),
    (T.TRADE, S.LIQUIDATE): _c({IN, OUT}, REQ, "清算人视角：付出还款资产、得到抵押凭证"),
    (T.BORROW, S.GENERATE_DEBT): _c({IN}, REQ, "借入资产，负债增加"),
    (T.REPAY, S.PAYBACK_DEBT): _c({OUT, NEUTRAL}, OPT, "偿还负债；被别人代还时借款人视角没有资产流动（方向 neutral）"),
    (T.REPAY, S.LIQUIDATE): _c({NEUTRAL}, NO, "被清算时负债减少（状态事件，数量为负债减少额）"),
    (T.SPEND, S.LIQUIDATE): _c({OUT}, REQ, "被清算时抵押凭证被强制转走"),
    (T.CLAIM, S.REWARD): _c({IN}, REQ, "领取协议奖励"),
    (T.CLAIM, S.LP_FEE): _c({IN}, REQ, "领取 LP 手续费"),
    (T.CLAIM, S.INTEREST): _c({IN}, REQ, "领取单独发放的利息"),
    (T.MINT, S.NONE): _c({IN}, REQ, "仓位 NFT 铸造"),
    (T.BURN, S.NONE): _c({OUT}, REQ, "仓位 NFT 销毁"),
    (T.STAKE, S.DEPOSIT_ASSET): _c({OUT}, REQ, "质押资产"),
    (T.UNSTAKE, S.REMOVE_ASSET): _c({IN}, REQ, "解押取回资产"),
    (T.BRIDGE, S.DEPOSIT_ASSET): _c({OUT}, REQ, "跨链转出"),
    (T.BRIDGE, S.REMOVE_ASSET): _c({IN}, REQ, "跨链转入（含 L2 的 L1 存款）"),
    (T.FEE, S.NONE): _c({OUT}, REQ, "交易 gas 费（含 L2 的 L1 数据费）"),
    (T.FEE, S.PROTOCOL_FEE): _c({OUT}, REQ, "协议另收的费用"),
    (T.INFORMATIONAL, S.APPROVE): _c({NEUTRAL}, NO, "授权，数量写在 extra"),
    (T.INFORMATIONAL, S.DECODED_LOG): _c({NEUTRAL}, NO, "未知合约的可读事件，事件名和参数写在 extra"),
    (T.INFORMATIONAL, S.NONE): _c({NEUTRAL}, NO, "其他不动资产的交互（签到等）"),
}


class TaxonomyError(ValueError):
    """事件不符合分类表：组合不存在、方向不允许，或资产流水要求不满足。"""


def category_of(event_type: EventType, event_subtype: EventSubtype) -> Category:
    """返回组合的定义。

    @raises TaxonomyError 组合不在分类表里
    """
    try:
        return CATEGORIES[(event_type, event_subtype)]
    except KeyError:
        raise TaxonomyError(f"分类表里没有组合 {event_type.value}/{event_subtype.value}") from None


def validate_event(event: NormalizedEvent) -> None:
    """检查一条事件是否符合分类表。

    @raises TaxonomyError 组合不存在、方向不允许，或资产流水要求不满足
    """
    cat = category_of(event.event_type, event.event_subtype)
    combo = f"{event.event_type.value}/{event.event_subtype.value}"
    if event.direction not in cat.directions:
        allowed = sorted(d.value for d in cat.directions)
        raise TaxonomyError(f"{combo} 不允许方向 {event.direction.value}，只允许 {allowed}")
    has_flows = bool(event.claimed_flow_ids)
    if cat.flows is FlowRule.REQUIRED and not has_flows:
        raise TaxonomyError(f"{combo} 必须认领资产流水")
    if cat.flows is FlowRule.FORBIDDEN and has_flows:
        raise TaxonomyError(f"{combo} 是状态或信息事件，不能认领资产流水")
    if has_flows and event.direction is Direction.NEUTRAL:
        raise TaxonomyError(f"{combo} 认领了资产流水，方向不能是 neutral")
