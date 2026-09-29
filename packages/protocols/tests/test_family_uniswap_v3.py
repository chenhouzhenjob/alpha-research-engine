"""uniswap_v3_like 家族：NPM 仓位的开仓、增减流动性、领取，三条链、两个分叉（PancakeSwap、Uniswap）。"""

from __future__ import annotations

import pytest
from _golden import load
from alpha_core.types import Chain
from alpha_protocols.decoding.models import NATIVE, Confidence, Direction, EventSubtype, EventType, FlowSource
from alpha_protocols.families.uniswap_v3_like.calls import parse_npm_calls
from alpha_protocols.families.uniswap_v3_like.decoder import DECREASE, NPM_COLLECT
from alpha_protocols.runtime import decode_context, decode_tx

V3_CASES = [
    "bsc/uniswap_v3_like/mint_token",
    "bsc/uniswap_v3_like/mint_native",
    "bsc/uniswap_v3_like/exit_multicall",
    "bsc/uniswap_v3_like/exit_multicall_unwrap",
    "bsc/uniswap_v3_like/collect_only",
    "ethereum/uniswap_v3_like/mint",
    "ethereum/uniswap_v3_like/exit_multicall_unwrap",
    "ethereum/uniswap_v3_like/collect_only",
    "base/uniswap_v3_like/mint",
    "base/uniswap_v3_like/exit_multicall_unwrap",
    "base/uniswap_v3_like/collect_only",
]


def _decode(case_id):
    s = load(case_id)
    return s, decode_tx(Chain(s.chain), s.tx, s.receipt, s.subject, ctx=decode_context(Chain(s.chain), tokens=s.tokens))


def _v3(d):
    return [e for e in d.events if e.family == "uniswap_v3_like"]


def _npm_amounts(s, topic0):
    """NPM 事件里的 (tokenId, amount0, amount1)。

    DecreaseLiquidity 的 data 是 (liquidity, a0, a1)，Collect 是 (recipient, a0, a1)。
    """
    lg = next(lg for lg in s.receipt.logs if lg.topics[:1] == [topic0])
    w = [int(lg.data[2 + i * 64 : 2 + (i + 1) * 64], 16) for i in range(3)]
    return int(lg.topics[1], 16), w[1], w[2]


@pytest.mark.parametrize("case_id", V3_CASES)
def test_every_v3_sample_is_decoded_by_family_with_chain_scoped_position_key(case_id):
    s, d = _decode(case_id)
    events = _v3(d)
    assert events, "V3 样本必须由家族解码，不能落到兜底"
    instance = "pancakeswap-v3" if s.chain == "bsc" else "uniswap-v3"
    assert all(e.instance_key == instance for e in events)
    keys = {e.position_key for e in events if e.position_key}
    assert keys and all(k.startswith(f"{s.chain}:{instance}:nft:") for k in keys)


def test_mint_token_opens_position_with_tick_range():
    s, d = _decode("bsc/uniswap_v3_like/mint_token")
    [mint] = [e for e in _v3(d) if e.event_type is EventType.MINT]
    deposits = [e for e in _v3(d) if e.event_type is EventType.DEPOSIT]
    assert mint.direction is Direction.IN and mint.token_id is not None
    assert len(deposits) == 2 and {e.position_key for e in deposits} == {mint.position_key}
    extra = deposits[0].extra
    assert extra["tick_lower"] < extra["tick_upper"] and extra["pool"].startswith("0x")
    assert {extra["token0"], extra["token1"]} == {e.asset for e in deposits}


def test_mint_native_deposits_value_and_infers_refund():
    s, d = _decode("bsc/uniswap_v3_like/mint_native")
    [deposit] = [e for e in _v3(d) if e.event_type is EventType.DEPOSIT and e.asset == NATIVE]
    [refund] = [e for e in _v3(d) if e.event_type is EventType.WITHDRAWAL]
    assert deposit.amount_raw == s.tx.value
    assert refund.extra == {"refund": True} and refund.confidence is Confidence.INFERRED
    assert refund.amount_raw == 42908340553887  # value − NPM 的 WBNB Deposit，规划 5.5 逐 wei 核对过


def test_exit_splits_principal_and_fee_by_decrease_amounts():
    """基准钱包的退出：同一笔交易 decrease + collect，每个 token 拆成本金和手续费。"""
    s, d = _decode("bsc/uniswap_v3_like/exit_multicall")
    _, d0, d1 = _npm_amounts(s, DECREASE)
    _, c0, c1 = _npm_amounts(s, NPM_COLLECT)
    by_index: dict = {}
    for e in _v3(d):
        by_index.setdefault(e.extra.get("token_index"), []).append(e)
    for i, (dec, col) in enumerate([(d0, c0), (d1, c1)]):
        if col == 0:
            continue
        principal = sum(e.amount_raw for e in by_index[i] if e.event_type is EventType.WITHDRAWAL)
        fee = sum(e.amount_raw for e in by_index[i] if e.event_subtype is EventSubtype.LP_FEE)
        assert (principal, fee) == (dec, col - dec)
        assert not any(e.extra.get("split_deferred") for e in by_index[i])
    # 拆分后的子流水记录父流水，父流水不重复计入
    children = [f for f in d.flows if f.parent_flow_id is not None]
    assert children and all(f.source is FlowSource.LOG for f in children)


def test_collect_only_is_fee_with_deferred_split():
    _, d = _decode("bsc/uniswap_v3_like/collect_only")
    events = [e for e in _v3(d) if e.claimed_flow_ids]
    assert events and all(e.event_subtype is EventSubtype.LP_FEE and e.extra["split_deferred"] for e in events)


@pytest.mark.parametrize("chain", ["bsc", "ethereum", "base"])
def test_exit_with_unwrap_infers_native_from_npm(chain):
    s, d = _decode(f"{chain}/uniswap_v3_like/exit_multicall_unwrap")
    native = [e for e in _v3(d) if e.asset == NATIVE]
    assert native and all(e.direction is Direction.IN and e.confidence is Confidence.INFERRED for e in native)
    assert s.subject in parse_npm_calls(s.tx.input).unwrap_recipients


def test_parse_npm_calls_handles_both_multicall_forms():
    s = load("ethereum/uniswap_v3_like/exit_multicall_unwrap")
    calls = parse_npm_calls(s.tx.input)
    assert calls.unwrap_recipients == (s.subject,)
    assert calls.sweeps and calls.sweeps[0][1] == s.subject
