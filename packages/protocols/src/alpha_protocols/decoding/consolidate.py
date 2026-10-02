"""交易级整合原语（与链无关的纯函数，规划 5.4 第 ③ 步）。

这里只放不含协议知识的通用算法，供家族调用：按资产算钱包净额、把净额拆成交换的付出腿和收到腿。
路由类家族（V2 路由、聚合器）共用它，不各写一套。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .models import AssetFlow, AssetFlowKind


@dataclass(frozen=True)
class NetPosition:
    """钱包在一笔交易里对一种资产的净额。"""

    asset: str
    token_id: int | None  # NFT 按 tokenId 分开算；其他为 None
    net_raw: int  # 正数为净流入，负数为净流出
    flow_ids: tuple[int, ...]  # 参与计算的流水


def net_by_asset(flows: Iterable[AssetFlow], subject: str) -> list[NetPosition]:
    """按 (资产, tokenId) 汇总钱包的净额，不含 gas；结果按资产排序，净额为 0 的也保留（调用方决定怎么处理）。"""
    acc: dict[tuple[str, int | None], tuple[int, list[int]]] = {}
    for f in flows:
        if f.kind is AssetFlowKind.GAS or subject not in (f.from_address, f.to_address):
            continue
        delta = (f.amount_raw if f.to_address == subject else 0) - (f.amount_raw if f.from_address == subject else 0)
        net, ids = acc.get((f.asset, f.token_id), (0, []))
        acc[(f.asset, f.token_id)] = (net + delta, [*ids, f.flow_id])
    return [
        NetPosition(a, t, net, tuple(ids))
        for (a, t), (net, ids) in sorted(acc.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0))
    ]


@dataclass(frozen=True)
class TradeLegs:
    """一次交换拆出的两组腿。"""

    spend: tuple[NetPosition, ...]  # 净流出的资产
    receive: tuple[NetPosition, ...]  # 净流入的资产
    netted_out: tuple[NetPosition, ...]  # 进出相抵、净额为 0 的资产（例如路由先转入再转出的中间 token）


def trade_from_net(positions: Iterable[NetPosition]) -> TradeLegs:
    """把净额拆成交换的付出腿和收到腿。"""
    items = list(positions)
    return TradeLegs(
        spend=tuple(p for p in items if p.net_raw < 0),
        receive=tuple(p for p in items if p.net_raw > 0),
        netted_out=tuple(p for p in items if p.net_raw == 0),
    )
