"""事件分类表与模型的约束。"""

from __future__ import annotations

import pytest
from alpha_protocols.decoding.models import (
    AssetFlow,
    AssetFlowKind,
    CoverageTier,
    Direction,
    EventSubtype,
    EventType,
    FlowSource,
    NormalizedEvent,
    PositionKind,
    PositionRef,
)
from alpha_protocols.decoding.taxonomy import CATEGORIES, FlowRule, TaxonomyError, category_of, validate_event


def _event(event_type, subtype, direction, flows=(1,)):
    return NormalizedEvent(
        chain="bsc",
        tx_hash="0x" + "11" * 32,
        seq=0,
        subject_wallet="0x" + "22" * 20,
        event_type=event_type,
        event_subtype=subtype,
        direction=direction,
        decoder_version="generic@1",
        coverage_tier=CoverageTier.T0,
        claimed_flow_ids=flows,
    )


def test_every_event_type_has_at_least_one_combination():
    """分类表一次写全：每个事件类型都至少有一个合法组合，没有"登记了类型却用不了"的情况。"""
    assert {t for t, _ in CATEGORIES} == set(EventType)


def test_every_subtype_is_used():
    assert {s for _, s in CATEGORIES} == set(EventSubtype)


def test_every_category_has_meaning_and_consistent_flow_rule():
    for (event_type, subtype), cat in CATEGORIES.items():
        assert cat.meaning, (event_type, subtype)
        assert cat.directions, (event_type, subtype)
        # 必须认领流水的组合不能允许 neutral；不能认领流水的组合只能是 neutral
        if cat.flows is FlowRule.REQUIRED:
            assert Direction.NEUTRAL not in cat.directions, (event_type, subtype)
        if cat.flows is FlowRule.FORBIDDEN:
            assert cat.directions == {Direction.NEUTRAL}, (event_type, subtype)


def test_asset_to_wrapped_symmetry():
    """存入协议 = deposit/deposit_asset（out）+ receive/receive_wrapped（in）；取回反过来。"""
    assert category_of(EventType.DEPOSIT, EventSubtype.DEPOSIT_ASSET).directions == {Direction.OUT}
    assert category_of(EventType.RECEIVE, EventSubtype.RECEIVE_WRAPPED).directions == {Direction.IN}
    assert category_of(EventType.SPEND, EventSubtype.RETURN_WRAPPED).directions == {Direction.OUT}
    assert category_of(EventType.WITHDRAWAL, EventSubtype.REMOVE_ASSET).directions == {Direction.IN}


def test_validate_accepts_legal_and_state_events():
    validate_event(_event(EventType.TRADE, EventSubtype.SPEND, Direction.OUT))
    validate_event(_event(EventType.REPAY, EventSubtype.LIQUIDATE, Direction.NEUTRAL, flows=()))
    # 被代还：借款人视角没有流水
    validate_event(_event(EventType.REPAY, EventSubtype.PAYBACK_DEBT, Direction.NEUTRAL, flows=()))
    # 自己还款：有流水
    validate_event(_event(EventType.REPAY, EventSubtype.PAYBACK_DEBT, Direction.OUT))


@pytest.mark.parametrize(
    ("event_type", "subtype", "direction", "flows", "message"),
    [
        (EventType.TRADE, EventSubtype.LP_FEE, Direction.IN, (1,), "没有组合"),
        (EventType.TRADE, EventSubtype.SPEND, Direction.IN, (1,), "不允许方向"),
        (EventType.CLAIM, EventSubtype.REWARD, Direction.IN, (), "必须认领"),
        (EventType.INFORMATIONAL, EventSubtype.APPROVE, Direction.NEUTRAL, (3,), "不能认领"),
        (EventType.REPAY, EventSubtype.PAYBACK_DEBT, Direction.NEUTRAL, (3,), "不能是 neutral"),
    ],
)
def test_validate_rejects_illegal(event_type, subtype, direction, flows, message):
    with pytest.raises(TaxonomyError, match=message):
        validate_event(_event(event_type, subtype, direction, flows))


def test_position_key_includes_chain():
    ref = PositionRef("base", "uniswap-v3", PositionKind.NFT, "123456", "0x" + "33" * 20)
    assert ref.key == "base:uniswap-v3:nft:123456"


def test_asset_flow_rejects_negative_amount():
    with pytest.raises(ValueError, match="不能为负"):
        AssetFlow(0, AssetFlowKind.ERC20, "0xabc", -1, "0x1", "0x2", FlowSource.LOG, log_index=0)
