"""DEX 聚合器的钱包视角解码：把一笔交换归并成 trade 的付出腿和收到腿（纯函数，规划 5.8）。

聚合器的路由细节不可知，也不需要知道：钱包视角只关心这笔交易付出了什么、收到了什么。
钱包发起、调用实例登记的路由、执行成功时处理；按资产算钱包的净额（`consolidate.trade_from_net`），
只认领净方向上的流水，风险 token（仿冒、垃圾空投）不参与：

- 有付出也有收到：不管调用的是哪个方法，都是一次交换，记 `trade/spend` + `trade/receive`，extra 记方法选择器；
- 只有付出、方法是登记的交换方法：换出的资产钱包看不到（换成原生币且缺内部交易数据，或走了原生币池），
  记 `trade/spend` 并标 extra.incomplete，告警 `internal_unavailable`；
- 只有单边、方法不是交换：用途不明（基准钱包的路由同时有跨链方法，例如 USDT 只出或只进），不认领，
  资产流动由兜底记录，告警 `unrecognized_call`。

原生币**不推断**：聚合器内部解包 WBNB 后把原生币转给谁、转多少不可知（样本 `swap_810c705b` 里聚合器解包了
0.003118 WBNB，钱包却没有收到任何原生币，规划 5.5）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...decoding.consolidate import net_by_asset, trade_from_net
from ...decoding.evm.dispatch import FamilyRun
from ...decoding.models import AssetFlowKind, Direction, EventSubtype, EventType, RiskFlag, WarningCode


@dataclass(frozen=True)
class DexAggregatorDecoder:
    family: str
    instance_key: str
    decoder_version: str
    routers: frozenset[str]
    swap_selectors: frozenset[str]  # 小写、带 0x 的 4 字节选择器

    def decode(self, run: FamilyRun) -> None:
        tx = run.tx
        if not run.is_call_target or tx.to_address not in self.routers or tx.from_address != run.subject:
            return
        selector = tx.method_selector
        flows = [
            f
            for f in run.ledger.unclaimed()
            if f.kind is not AssetFlowKind.GAS and run.ctx.risk_of(f.asset) in (RiskFlag.NORMAL, RiskFlag.HACKED)
        ]
        legs = trade_from_net(net_by_asset(flows, run.subject))
        if not legs.spend and not legs.receive:
            return  # 没有资产流动（授权、签到之类），不是交换
        two_sided = bool(legs.spend) and bool(legs.receive)
        if not two_sided and selector not in self.swap_selectors:
            run.warn(
                WarningCode.UNRECOGNIZED_CALL, f"方法 {selector} 不是登记的交换方法，且只有单边资产流动（可能是跨链）"
            )
            return
        incomplete = not two_sided
        if incomplete:
            run.warn(WarningCode.INTERNAL_UNAVAILABLE, f"方法 {selector} 只有付出腿，换出的资产看不到")
        extra = {"selector": selector} | ({"incomplete": True} if incomplete else {})
        by_id = {f.flow_id: f for f in flows}
        for positions, subtype, outgoing in (
            (legs.spend, EventSubtype.SPEND, True),
            (legs.receive, EventSubtype.RECEIVE, False),
        ):
            for p in positions:
                ids = [i for i in p.flow_ids if (by_id[i].from_address == run.subject) == outgoing]
                if ids:
                    run.claim_event(
                        EventType.TRADE,
                        subtype,
                        Direction.OUT if outgoing else Direction.IN,
                        ids,
                        counterparty=tx.to_address,
                        extra=extra,
                    )
