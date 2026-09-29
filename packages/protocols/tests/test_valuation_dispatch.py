"""估值调度：多轮读取、嵌套解包（用合成估值器，首批家族里没有真实的嵌套持仓）。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from alpha_protocols.decoding.models import PositionKind, PositionRef
from alpha_protocols.valuation.dispatch import MAX_NESTING, ValuationError, value_positions
from alpha_protocols.valuation.models import Component, StateRead, UnderlyingAmount, Valuation, ValuationRequest

VAULT, LP, USDT, CAKE = "vault", "lp-token", "usdt", "cake"


@dataclass
class _ShareValuer:
    """份额 → 底层：每个份额值 `rate` 个 `underlying`（汇率要先读一轮）。"""

    instance_key: str
    underlying: str
    rate: int
    component: Component = Component.PRINCIPAL

    def plan(self, request, reads):
        key = f"{self.instance_key}:rate"
        return [] if key in reads else [StateRead(key, "0x" + "00" * 20, "0x")]

    def unwrap(self, request, reads):
        amount = request.amount_raw if request.amount_raw is not None else 10
        return Valuation(request, (UnderlyingAmount(self.underlying, amount * self.rate, self.component),))


def _reader(batch):
    return {r.key: b"\x01" for r in batch}


def _request(instance, owner="0xme"):
    return ValuationRequest(PositionRef("bsc", instance, PositionKind.SHARE, "x", owner))


def test_nested_share_is_expanded_recursively():
    """金库份额 → LP → USDT：两层嵌套都解开，数量按汇率相乘。"""
    valuers = {"vault": _ShareValuer("vault", LP, 3), "lp": _ShareValuer("lp", USDT, 7)}
    [val] = value_positions([_request("vault")], valuers, _reader, share_tokens={LP: "lp"})
    assert [(a.asset, a.amount_raw, a.component) for a in val.amounts] == [(USDT, 10 * 3 * 7, Component.PRINCIPAL)]
    assert val.extra["nested"] == {"bsc:lp:share:lp-token": "ok"}


def test_fee_paid_in_share_keeps_fee_component_after_expansion():
    valuers = {"vault": _ShareValuer("vault", LP, 2, Component.FEE), "lp": _ShareValuer("lp", USDT, 5)}
    [val] = value_positions([_request("vault")], valuers, _reader, share_tokens={LP: "lp"})
    assert [(a.asset, a.component) for a in val.amounts] == [(USDT, Component.FEE)]


def test_nesting_depth_is_capped():
    chain = {f"s{i}": _ShareValuer(f"s{i}", f"t{i + 1}", 1) for i in range(MAX_NESTING + 2)}
    shares = {f"t{i}": f"s{i}" for i in range(1, MAX_NESTING + 3)}
    [val] = value_positions([_request("s0")], chain, _reader, share_tokens=shares)
    assert [a.asset for a in val.amounts] == [f"t{MAX_NESTING}"], "超过最大深度的份额按原样保留"


def test_non_converging_plan_raises():
    class _Loop(_ShareValuer):
        def plan(self, request, reads):
            return [StateRead(f"k{len(reads)}", "0x" + "00" * 20, "0x")]

    with pytest.raises(ValuationError, match="读取计划"):
        value_positions([_request("loop")], {"loop": _Loop("loop", USDT, 1)}, _reader)


def test_missing_valuer_is_reported_not_raised():
    [val] = value_positions([_request("unknown")], {}, _reader)
    assert val.amounts == () and "没有估值器" in val.error
