"""V3 仓位估值：回放固定区块上的真实读取，与链上静态调用的真值逐 wei 比较。

真值来自以 owner 身份静态调用 NPM：`decreaseLiquidity`（全部流动性）的返回值是本金，
`collect`（最大数量）的返回值是可领手续费。样本由 scripts/oneoff/2026-09-29_m2-valuation-samples.py 生成。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alpha_core.types import Chain
from alpha_protocols.decoding.models import PositionKind, PositionRef
from alpha_protocols.families.uniswap_v3_like.math import MAX_TICK, MIN_TICK, Q96, get_sqrt_ratio_at_tick
from alpha_protocols.runtime import value
from alpha_protocols.valuation.models import Component, ValuationRequest

SAMPLES = sorted(
    p
    for p in (Path(__file__).parent / "valuation_golden").glob("*/*.json")
    if json.loads(p.read_text()).get("kind") != "v2_burn"
)


def _replay_reader(doc):
    reads = {k: (bytes.fromhex(v) if v is not None else None) for k, v in doc["reads"].items()}

    def read(batch):
        missing = [r.key for r in batch if r.key not in reads]
        assert not missing, f"估值器请求了样本里没有的读取：{missing}"
        return {r.key: reads[r.key] for r in batch}

    return read


def test_samples_cover_in_and_out_of_range_on_every_chain():
    by_chain: dict = {}
    for p in SAMPLES:
        doc = json.loads(p.read_text())
        by_chain.setdefault(doc["chain"], set()).add(doc["in_range"])
    assert set(by_chain) == {"bsc", "ethereum", "base"}
    assert all(v == {True, False} for v in by_chain.values()), by_chain


@pytest.mark.parametrize("path", SAMPLES, ids=[f"{p.parent.name}/{p.stem}" for p in SAMPLES])
def test_valuation_equals_onchain_static_calls(path):
    doc = json.loads(path.read_text())
    position = PositionRef(doc["chain"], doc["instance_key"], PositionKind.NFT, str(doc["token_id"]), doc["owner"])
    [val] = value(Chain(doc["chain"]), [ValuationRequest(position)], _replay_reader(doc))
    assert val.error is None
    principal = [a.amount_raw for a in val.amounts if a.component is Component.PRINCIPAL]
    fees = [a.amount_raw for a in val.amounts if a.component is Component.FEE]
    assert principal == doc["truth"]["principal"]
    assert fees == doc["truth"]["fees"]
    assert val.extra["in_range"] is doc["in_range"]


def test_tick_math_reference_values():
    """TickMath 合约里的边界常数。"""
    assert get_sqrt_ratio_at_tick(0) == Q96
    assert get_sqrt_ratio_at_tick(MIN_TICK) == 4295128739
    assert get_sqrt_ratio_at_tick(MAX_TICK) == 1461446703485210103287273052203988822378723970342
    with pytest.raises(ValueError):
        get_sqrt_ratio_at_tick(MAX_TICK + 1)
