"""DEX 聚合器的钱包视角解码：把一笔交换归并成 trade 的付出腿和收到腿（纯函数，规划 5.8）。

聚合器的路由细节不可知，也不需要知道：钱包视角只关心这笔交易付出了什么、收到了什么。
触发条件：交易由钱包发起、调用的是实例登记的路由、方法选择器在实例配置的 `swap_selectors` 里、执行成功。

归并：按资产算钱包的净额（`consolidate.trade_from_net`），净流出记 `trade/spend`、净流入记 `trade/receive`，
只认领净方向上的流水；风险 token（仿冒、垃圾空投）不参与。

原生币：聚合器内部解包 WBNB 后把原生币转给谁、转多少不可知（样本 `swap_810c705b` 里聚合器解包了
0.003118 WBNB，钱包却没有收到任何原生币），所以**不推断**（规划 5.5）。只有付出腿、没有收到腿且交易里
有包装原生币的 Withdrawal 时，大概率是换成了原生币但缺内部交易数据：告警 `internal_unavailable`，
付出腿带 `extra.incomplete`。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...decoding.consolidate import net_by_asset, trade_from_net
from ...decoding.evm.dispatch import FamilyRun
from ...decoding.evm.flows import event_topic
from ...decoding.models import AssetFlowKind, Direction, EventSubtype, EventType, RiskFlag, WarningCode

WITHDRAWAL = event_topic("Withdrawal(address,uint256)")


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
        if tx.method_selector not in self.swap_selectors:
            return
        flows = [
            f
            for f in run.ledger.unclaimed()
            if f.kind is not AssetFlowKind.GAS and run.ctx.risk_of(f.asset) in (RiskFlag.NORMAL, RiskFlag.HACKED)
        ]
        legs = trade_from_net(net_by_asset(flows, run.subject))
        incomplete = bool(legs.spend) and not legs.receive and self._unwrapped(run)
        if incomplete:
            run.warn(WarningCode.INTERNAL_UNAVAILABLE, "只有付出腿，交易里有 WETH 解包：换出的原生币缺内部交易数据")
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
                        extra={"incomplete": True} if incomplete and outgoing else None,
                    )

    @staticmethod
    def _unwrapped(run: FamilyRun) -> bool:
        wrapped = run.ctx.wrapped_native
        return any(lg.address == wrapped and lg.topics[:1] == [WITHDRAWAL] for lg in run.receipt.logs)
