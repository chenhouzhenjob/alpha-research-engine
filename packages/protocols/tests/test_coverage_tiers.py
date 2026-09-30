"""覆盖等级（设计文档 3.3）：家族事件是 T2，持仓能按家族估值时升到 T3；兜底事件是 T0。"""

from __future__ import annotations

import dataclasses

import pytest
from _golden import all_case_ids, discovered_identities, load
from alpha_core.types import Chain
from alpha_protocols.decoding.models import CoverageTier, PositionKind, PositionRef
from alpha_protocols.runtime import decode_context, decode_tx

FAMILY_CASES = [c for c in all_case_ids() if c.split("/")[1] != "generic"]


def _decode(case_id, **ctx_overrides):
    s = load(case_id)
    chain = Chain(s.chain)
    ctx = decode_context(chain, tokens=s.tokens, extra_identities=discovered_identities(s.chain))
    ctx = dataclasses.replace(ctx, **ctx_overrides)
    return decode_tx(chain, s.tx, s.receipt, s.subject, ctx=ctx)


def test_position_kind_is_parsed_from_key():
    assert PositionRef.kind_of("bsc:venus-core:debt:0xabc") is PositionKind.DEBT
    with pytest.raises(ValueError):
        PositionRef.kind_of("bsc:venus-core")


def test_runtime_context_lists_valuable_kinds_per_instance():
    ctx = decode_context(Chain.BSC)
    assert ctx.valuable_positions["pancakeswap-v3"] == {PositionKind.NFT}
    assert ctx.valuable_positions["pancakeswap-v2"] == {PositionKind.SHARE}
    assert ctx.valuable_positions["venus-core"] == {PositionKind.SHARE, PositionKind.DEBT, PositionKind.CLAIMABLE}
    assert "aggregator-b300" not in ctx.valuable_positions  # 聚合器没有持仓，不估值


@pytest.mark.parametrize("case_id", FAMILY_CASES)
def test_family_events_are_t3_iff_position_is_valuable(case_id):
    d = _decode(case_id)
    for e in d.events:
        if e.family is None:
            assert e.coverage_tier is CoverageTier.T0
        elif e.position_key is not None:
            assert e.coverage_tier is CoverageTier.T3, e
        else:
            assert e.coverage_tier is CoverageTier.T2, e  # 交换、授权这类没有持仓的事件


def test_v3_mint_is_t3_and_aggregator_trade_stays_t2():
    mint = _decode("bsc/uniswap_v3_like/mint_token")
    assert {e.coverage_tier for e in mint.events if e.family == "uniswap_v3_like"} == {CoverageTier.T3}
    swap = _decode("bsc/dex_aggregator/call_lifi_swap")
    assert {e.coverage_tier for e in swap.events if e.family == "dex_aggregator"} == {CoverageTier.T2}


def test_without_valuers_family_events_stay_t2():
    d = _decode("bsc/uniswap_v3_like/mint_token", valuable_positions={})
    assert {e.coverage_tier for e in d.events if e.family == "uniswap_v3_like"} == {CoverageTier.T2}
