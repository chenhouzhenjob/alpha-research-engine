"""compound_v2_like 家族（Venus 核心池）：存款、取款、借款、还款、代还、清算、领奖励，以及估值。

市场识别结果来自固定区块上 Comptroller.getAllMarkets() 的清单（tests/fixtures/venus_core_markets.json），
等价于识别第一层的 registry_call 已经跑过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _golden import discovered_identities, load
from alpha_core.types import Chain
from alpha_protocols.decoding.models import (
    NATIVE,
    Confidence,
    Direction,
    EventSubtype,
    EventType,
    PositionKind,
    PositionRef,
)
from alpha_protocols.runtime import decode_context, decode_tx, value
from alpha_protocols.valuation.models import Component, ValuationRequest

VUSDT = "0xfd5840cd36d94d7229439859c0112a4185bc0255"
VBNB = "0xa07c5b74c9b40447a954e1466938b865b6bbea36"
XVS = "0xcf6bb5389c92bdda8a3747ddb454cb7a64626c63"
SAMPLES = sorted(p for p in (Path(__file__).parent / "valuation_golden" / "bsc").glob("venus-core_*.json"))


def _decode(case):
    s = load(f"bsc/compound_v2_like/{case}")
    ctx = decode_context(Chain.BSC, tokens=s.tokens, extra_identities=discovered_identities("bsc"))
    return s, decode_tx(Chain.BSC, s.tx, s.receipt, s.subject, ctx=ctx)


def _venus(d):
    return [e for e in d.events if e.family == "compound_v2_like"]


def _kinds(events):
    return sorted((e.event_type.value, e.event_subtype.value, e.direction.value) for e in events)


def test_supply_and_redeem_vusdt_with_venus_four_field_events():
    _, d = _decode("supply_vusdt")
    assert _kinds(_venus(d)) == [("deposit", "deposit_asset", "out"), ("receive", "receive_wrapped", "in")]
    assert {e.position_key for e in _venus(d)} == {f"bsc:venus-core:share:{VUSDT}"}
    _, d = _decode("redeem_vusdt")
    assert _kinds(_venus(d)) == [("spend", "return_wrapped", "out"), ("withdrawal", "remove_asset", "in")]


def test_vbnb_redeem_and_borrow_use_compound_three_field_events_and_infer_native():
    for case, kind in (("redeem_vbnb", EventType.WITHDRAWAL), ("borrow_vbnb", EventType.BORROW)):
        _, d = _decode(case)
        [native] = [e for e in _venus(d) if e.asset == NATIVE]
        assert native.event_type is kind and native.confidence is Confidence.INFERRED


def test_borrow_opens_debt_position():
    _, d = _decode("borrow_vusdt")
    [borrow] = _venus(d)
    assert (borrow.event_type, borrow.event_subtype) == (EventType.BORROW, EventSubtype.GENERATE_DEBT)
    assert borrow.position_key == f"bsc:venus-core:debt:{VUSDT}" and borrow.extra["account_borrows"] > 0


def test_own_repay_is_outgoing():
    _, d = _decode("repay_vusdt")
    [repay] = _venus(d)
    assert (repay.direction, repay.extra["on_behalf"]) == (Direction.OUT, False)


def test_repay_with_collateral_borrower_sees_state_event_without_flow():
    """借款人发起、辅助合约用它的 vUSDT 抵押赎回后替它还款：借款人这边负债减少是状态事件。"""
    s, d = _decode("repay_on_behalf_borrower")
    [state] = [e for e in _venus(d) if e.event_type is EventType.REPAY]
    assert state.direction is Direction.NEUTRAL and state.claimed_flow_ids == ()
    assert state.position_key == f"bsc:venus-core:debt:{VUSDT}" and state.asset is not None


def test_third_party_deposit_is_not_claimed_as_wallet_supply():
    """0x4d2e… 用用户的 BNB 存进 vBNB，但 vBNB 留在它自己名下：不是钱包的存款，Venus 不认领。"""
    _, d = _decode("supply_vbnb_via_gateway")
    assert _venus(d) == []


def test_liquidation_borrower_view():
    _, d = _decode("liquidation_borrower")
    kinds = _kinds(_venus(d))
    assert ("repay", "liquidate", "neutral") in kinds and ("spend", "liquidate", "out") in kinds


def test_liquidation_liquidator_view_gets_collateral():
    _, d = _decode("liquidation_liquidator")
    got = [e for e in _venus(d) if (e.event_type, e.direction) == (EventType.TRADE, Direction.IN)]
    assert got and got[0].asset == VBNB


def test_claim_xvs_is_reward():
    _, d = _decode("claim_xvs")
    [reward] = [e for e in _venus(d) if e.event_type is EventType.CLAIM]
    assert (reward.event_subtype, reward.asset) == (EventSubtype.REWARD, XVS)


# ----------------------------------------------------------------------
# 估值：与链上 balanceOfUnderlying / borrowBalanceCurrent / venusAccrued 相等
# ----------------------------------------------------------------------


def _replay(doc):
    reads = {k: (bytes.fromhex(v) if v is not None else None) for k, v in doc["reads"].items()}
    return lambda batch: {r.key: reads[r.key] for r in batch}


def test_valuation_samples_cover_every_position_kind():
    assert {json.loads(p.read_text())["position_kind"] for p in SAMPLES} == {"share", "debt", "claimable"}


@pytest.mark.parametrize("path", SAMPLES, ids=[p.stem for p in SAMPLES])
def test_venus_valuation_equals_onchain(path):
    doc = json.loads(path.read_text())
    kind = PositionKind(doc["position_kind"])
    position = PositionRef("bsc", "venus-core", kind, doc["target"], doc["owner"])
    [val] = value(Chain.BSC, [ValuationRequest(position)], _replay(doc))
    assert val.error is None
    [amount] = val.amounts
    assert amount.amount_raw == doc["truth"]["amount"]
    expected = {
        PositionKind.SHARE: (Component.PRINCIPAL, 1),
        PositionKind.DEBT: (Component.DEBT, -1),
        PositionKind.CLAIMABLE: (Component.REWARD, 1),
    }
    assert (amount.component, amount.sign) == expected[kind]
    assert amount.lower_bound is (kind is PositionKind.CLAIMABLE)
    if kind is PositionKind.DEBT:
        assert "shortfall" in val.extra
    if doc["target"] == VBNB and kind is not PositionKind.CLAIMABLE:
        assert amount.asset == NATIVE
