"""uniswap_v2_like 家族：经路由添加 / 移除流动性、交换的解码，LP 份额估值与链上 Burn 逐 wei 相等。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _golden import load
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
from alpha_protocols.families.uniswap_v2_like.valuation import protocol_fee_liquidity
from alpha_protocols.runtime import decode_context, decode_tx, value
from alpha_protocols.valuation.models import Component, ValuationRequest

INSTANCE = {"bsc": "pancakeswap-v2", "ethereum": "uniswap-v2", "base": "uniswap-v2"}
REMOVES = [
    "bsc/uniswap_v2_like/remove_liquidity",
    "bsc/uniswap_v2_like/remove_liquidity_eth",
    "ethereum/uniswap_v2_like/remove_liquidity_eth",
    "base/uniswap_v2_like/remove_liquidity_eth",
]
SWAPS = [
    "bsc/uniswap_v2_like/swap_tokens_for_tokens",
    "bsc/uniswap_v2_like/swap_eth_for_tokens",
    "bsc/uniswap_v2_like/swap_tokens_for_eth",
    "ethereum/uniswap_v2_like/swap_tokens_for_eth",
    "ethereum/uniswap_v2_like/swap_tokens_for_eth_tax_token",
    "base/uniswap_v2_like/swap_tokens_for_eth",
]
BURNS = sorted(
    p
    for p in (Path(__file__).parent / "valuation_golden").glob("*/*.json")
    if json.loads(p.read_text()).get("kind") == "v2_burn"
)


def _decode(case_id):
    s = load(case_id)
    return s, decode_tx(Chain(s.chain), s.tx, s.receipt, s.subject, ctx=decode_context(Chain(s.chain), tokens=s.tokens))


def _v2(d):
    return [e for e in d.events if e.family == "uniswap_v2_like"]


def _kinds(events):
    return sorted((e.event_type.value, e.event_subtype.value, e.direction.value) for e in events)


@pytest.mark.parametrize("chain", ["bsc", "ethereum", "base"])
def test_add_liquidity_deposits_tokens_and_receives_lp(chain):
    s, d = _decode(f"{chain}/uniswap_v2_like/add_liquidity")
    events = _v2(d)
    assert ("receive", "receive_wrapped", "in") in _kinds(events)
    assert [k for k in _kinds(events) if k[0] == "deposit"] == [("deposit", "deposit_asset", "out")] * 2
    keys = {e.position_key for e in events}
    assert len(keys) == 1 and next(iter(keys)).startswith(f"{chain}:{INSTANCE[chain]}:share:")


def test_add_liquidity_eth_pays_value_and_gets_refund():
    s, d = _decode("bsc/uniswap_v2_like/add_liquidity_eth")
    [paid] = [e for e in _v2(d) if e.event_type is EventType.DEPOSIT and e.asset == NATIVE]
    [refund] = [e for e in _v2(d) if e.extra.get("refund")]
    assert paid.amount_raw == s.tx.value and refund.amount_raw == 1  # 规划 5.5：退回 1 wei


@pytest.mark.parametrize("case_id", REMOVES)
def test_remove_liquidity_returns_lp_and_withdraws(case_id):
    s, d = _decode(case_id)
    kinds = _kinds(_v2(d))
    assert ("spend", "return_wrapped", "out") in kinds
    assert kinds.count(("withdrawal", "remove_asset", "in")) == 2
    if case_id.endswith("_eth"):
        [native] = [e for e in _v2(d) if e.asset == NATIVE]
        assert native.confidence is Confidence.INFERRED


@pytest.mark.parametrize("case_id", SWAPS)
def test_swap_has_one_spend_and_one_receive_asset(case_id):
    _, d = _decode(case_id)
    trades = [e for e in _v2(d) if e.event_type is EventType.TRADE]
    spend = {e.asset for e in trades if e.event_subtype is EventSubtype.SPEND}
    receive = {e.asset for e in trades if e.event_subtype is EventSubtype.RECEIVE}
    assert len(spend) == 1 and len(receive) == 1 and spend != receive
    assert all(
        e.direction is (Direction.OUT if e.event_subtype is EventSubtype.SPEND else Direction.IN) for e in trades
    )


def test_tax_token_swap_receives_only_the_wallets_unwrap():
    """带转账税的 token：路由解包两次，钱包只收到第二次（规划 5.5）。"""
    _, d = _decode("ethereum/uniswap_v2_like/swap_tokens_for_eth_tax_token")
    [received] = [e for e in _v2(d) if e.event_subtype is EventSubtype.RECEIVE]
    assert (received.asset, received.amount_raw) == (NATIVE, 201145510641318173)


def test_protocol_fee_formula_matches_both_forks():
    # rootK = 200、rootKLast = 100、ts = 1000
    assert protocol_fee_liquidity(1000, 40000, 1, 100**2, 1, 5) == 1000 * 100 // (200 * 5 + 100)  # Uniswap
    assert protocol_fee_liquidity(1000, 40000, 1, 100**2, 8, 17) == 1000 * 100 * 8 // (200 * 17 + 100 * 8)  # Pancake
    assert protocol_fee_liquidity(1000, 40000, 1, 0, 1, 5) == 0  # kLast 为 0：没有协议费
    assert protocol_fee_liquidity(1000, 100, 100, 200**2, 1, 5) == 0  # rootK 没有增长


def _replay(doc):
    reads = {k: (bytes.fromhex(v) if v is not None else None) for k, v in doc["reads"].items()}
    return lambda batch: {r.key: reads[r.key] for r in batch}


def test_burn_samples_cover_every_chain():
    assert {json.loads(p.read_text())["chain"] for p in BURNS} == {"bsc", "ethereum", "base"}


@pytest.mark.parametrize("path", BURNS, ids=[f"{p.parent.name}/{p.stem}" for p in BURNS])
def test_lp_valuation_equals_onchain_burn(path):
    """交易前一个区块上，按交出的 LP 数量估值 = 交易对 Burn 事件的数量（含协议费稀释）。"""
    doc = json.loads(path.read_text())
    position = PositionRef(doc["chain"], doc["instance_key"], PositionKind.SHARE, doc["pair"], doc["owner"])
    [val] = value(Chain(doc["chain"]), [ValuationRequest(position, doc["liquidity"])], _replay(doc))
    assert val.error is None
    assert [a.amount_raw for a in val.amounts if a.component is Component.PRINCIPAL] == doc["truth"]["principal"]
