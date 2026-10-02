"""第二段分派、流水认领、推断钩子和 wrapped_native 家族。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from _golden import all_case_ids, discovered_identities, load
from alpha_core.types import Chain
from alpha_protocols.decoding.claims import DecodeConflictError, FlowLedger
from alpha_protocols.decoding.consolidate import net_by_asset, trade_from_net
from alpha_protocols.decoding.context import ContractIdentity
from alpha_protocols.decoding.evm.pipeline import decode_evm_tx
from alpha_protocols.decoding.models import (
    NATIVE,
    AssetFlow,
    AssetFlowKind,
    Confidence,
    Direction,
    EventSubtype,
    EventType,
    FlowSource,
    InternalTransfer,
    WarningCode,
)
from alpha_protocols.runtime import decode_context, decode_tx
from test_decoding_generic import NEEDS_INFERENCE, _native_net

ALL = all_case_ids()

# 接入 wrapped_native 家族后，钱包直接解包 WBNB/WETH 的样本能完整对上余额了
RESOLVED_BY_WRAPPED_NATIVE = {
    "bsc/wrapped_native/unwrap",
    "ethereum/wrapped_native/unwrap",
    "base/wrapped_native/unwrap",
}
# 接入 uniswap_v3_like 后：NPM 的 unwrapWETH9（三条链）和 refundETH 由家族推断，余额闭合
RESOLVED_BY_UNISWAP_V3 = {
    "bsc/uniswap_v3_like/exit_multicall_unwrap",
    "bsc/uniswap_v3_like/mint_native",
    "ethereum/uniswap_v3_like/exit_multicall_unwrap",
    "base/uniswap_v3_like/exit_multicall_unwrap",
}
# 接入 uniswap_v2_like 后：路由的原生币转出（含带转账税 token）和多付退款由家族推断，余额闭合
RESOLVED_BY_UNISWAP_V2 = {
    f"{chain}/uniswap_v2_like/{case}"
    for chain in ("bsc", "ethereum", "base")
    for case in ("remove_liquidity_eth", "swap_tokens_for_eth")
} | {"bsc/uniswap_v2_like/add_liquidity_eth", "ethereum/uniswap_v2_like/swap_tokens_for_eth_tax_token"}
# 接入 compound_v2_like 后：vBNB 的 Borrow / Redeem 由家族推断原生币，余额闭合。清算人样本在只接入
# wrapped_native 时曾暴露出一笔"看不见的资金来源"（清算人合约把 0.000583 BNB 包装成 WBNB）：那笔 BNB 正是
# 它把拿到的 vBNB 抵押品赎回得到的，Venus 家族推断出这笔赎回后缺口闭合。
RESOLVED_BY_COMPOUND_V2 = {
    "bsc/compound_v2_like/borrow_vbnb",
    "bsc/compound_v2_like/redeem_vbnb",
}
STILL_NEEDS_INFERENCE = (
    NEEDS_INFERENCE
    - RESOLVED_BY_WRAPPED_NATIVE
    - RESOLVED_BY_UNISWAP_V3
    - RESOLVED_BY_UNISWAP_V2
    - RESOLVED_BY_COMPOUND_V2
)
# 聚合器路由内部走向不透明，按设计不推断，等数据源的内部交易（规划 5.5）
assert {"bsc/dex_aggregator/swap_dad12b6c", "bsc/dex_aggregator/to_native"} == STILL_NEEDS_INFERENCE


def _decode(s, **kw):
    ctx = decode_context(Chain(s.chain), tokens=s.tokens, extra_identities=discovered_identities(s.chain))
    return decode_tx(Chain(s.chain), s.tx, s.receipt, s.subject, ctx=ctx, **kw)


@pytest.mark.parametrize("case_id", ALL)
def test_full_pipeline_reconciles_and_claims_everything(case_id):
    s = load(case_id)
    d = _decode(s)
    assert d.unclaimed_flow_ids == ()
    net = _native_net(d.flows, s.subject)
    if case_id in STILL_NEEDS_INFERENCE:
        assert net != s.balance_delta, "缺口消失了：更新 STILL_NEEDS_INFERENCE"
    else:
        assert net == s.balance_delta
    assert _decode(s) == d


@pytest.mark.parametrize("chain", ["bsc", "ethereum", "base"])
def test_wrap_is_asset_to_receipt_pair(chain):
    s = load(f"{chain}/wrapped_native/wrap")
    d = _decode(s)
    kinds = {(e.event_type, e.event_subtype, e.direction): e for e in d.events if e.family == "wrapped_native"}
    out = kinds[(EventType.DEPOSIT, EventSubtype.DEPOSIT_ASSET, Direction.OUT)]
    got = kinds[(EventType.RECEIVE, EventSubtype.RECEIVE_WRAPPED, Direction.IN)]
    assert out.asset == NATIVE and out.amount_raw == s.tx.value and out.confidence is Confidence.EXACT
    assert got.asset == s.tx.to_address and got.amount_raw == s.tx.value and got.confidence is Confidence.INFERRED
    assert out.instance_key == got.instance_key == "wrapped-native"


@pytest.mark.parametrize("chain", ["bsc", "ethereum", "base"])
def test_unwrap_infers_both_sides(chain):
    s = load(f"{chain}/wrapped_native/unwrap")
    d = _decode(s)
    wrapped = [e for e in d.events if e.family == "wrapped_native"]
    assert sorted((e.event_type.value, e.direction.value) for e in wrapped) == [("spend", "out"), ("withdrawal", "in")]
    assert len({e.amount_raw for e in wrapped}) == 1
    assert all(e.confidence is Confidence.INFERRED for e in wrapped)


def test_router_unwrap_is_not_claimed_by_wrapped_native():
    """NPM 替钱包解包时 Withdrawal 的 src 是 NPM：原生币怎么到钱包归 V3 家族推断，这里不认领。"""
    d = _decode(load("bsc/uniswap_v3_like/exit_multicall_unwrap"))
    assert not [e for e in d.events if e.family == "wrapped_native"]


def test_internal_data_replaces_native_inference():
    s = load("bsc/wrapped_native/unwrap")
    gap = (
        s.balance_delta
        - _native_net(_decode(s).flows, s.subject)
        + _native_net([f for f in _decode(s).flows if f.source is FlowSource.INFERRED], s.subject)
    )
    wbnb = s.tx.to_address
    matched = _decode(s, internal=[InternalTransfer(wbnb, s.subject, gap)])
    assert not [f for f in matched.flows if f.asset == NATIVE and f.source is FlowSource.INFERRED]
    assert _native_net(matched.flows, s.subject) == s.balance_delta
    # 数据源给了内部交易，但没有这笔：不补原生币，记告警
    mismatch = _decode(s, internal=[])
    assert any(w.code is WarningCode.INFERENCE_MISMATCH for w in mismatch.warnings)
    assert not [f for f in mismatch.flows if f.asset == NATIVE and f.source is FlowSource.INFERRED]


# ----------------------------------------------------------------------
# 分派顺序与认领冲突（假家族）
# ----------------------------------------------------------------------


@dataclass
class _Recorder:
    instance_key: str
    family: str = "fake_like"
    calls: list = field(default_factory=list)
    claim_first_flow: bool = False

    @property
    def decoder_version(self):
        return f"{self.family}@1"

    def decode(self, run):
        self.calls.append((self.instance_key, [lg.log_index for lg in run.logs], run.is_call_target))
        if self.claim_first_flow:
            f = run.ledger.flows[0]
            direction = Direction.OUT if f.from_address == run.subject else Direction.IN
            run.claim_event(EventType.TRANSFER, EventSubtype.NONE, direction, [f.flow_id])


def _fake_setup(s, *recorders_by_address):
    identities = {a: ContractIdentity(a, "pool", "fake_like", r.instance_key) for a, r in recorders_by_address}
    ctx = decode_context(Chain(s.chain), tokens=s.tokens, extra_identities=identities)
    return ctx, {r.instance_key: r for _, r in recorders_by_address}


def test_dispatch_calls_call_target_first_then_by_log_order():
    s = load("bsc/uniswap_v2_like/add_liquidity")
    emitters = []
    for lg in sorted(s.receipt.logs, key=lambda lg: lg.log_index):
        if lg.address not in emitters and lg.address != s.tx.to_address:
            emitters.append(lg.address)
    late, early, router = _Recorder("late"), _Recorder("early"), _Recorder("router")
    shared: list = []
    for r in (late, early, router):
        r.calls = shared
    ctx, decoders = _fake_setup(s, (emitters[-1], late), (emitters[0], early), (s.tx.to_address, router))
    decode_evm_tx(s.tx, s.receipt, s.subject, ctx, load(s.case_id).rules, decoders=decoders)
    assert [c[0] for c in shared] == ["router", "early", "late"]
    assert shared[0][2] is True and shared[1][2] is False


def test_two_decoders_claiming_same_flow_conflict():
    s = load("bsc/uniswap_v2_like/add_liquidity")  # 调用的是路由，日志由交易对和 token 发出
    emitter = next(lg.address for lg in s.receipt.logs if lg.address != s.tx.to_address)
    a, b = _Recorder("a", claim_first_flow=True), _Recorder("b", claim_first_flow=True)
    ctx, decoders = _fake_setup(s, (emitter, a), (s.tx.to_address, b))
    with pytest.raises(DecodeConflictError, match="又认领了一次"):
        decode_evm_tx(s.tx, s.receipt, s.subject, ctx, s.rules, decoders=decoders)


def test_failed_tx_is_not_dispatched():
    s = load("bsc/generic/failed")
    r = _Recorder("agg")
    ctx, decoders = _fake_setup(s, (s.tx.to_address, r))
    decode_evm_tx(s.tx, s.receipt, s.subject, ctx, s.rules, decoders=decoders)
    assert r.calls == []


# ----------------------------------------------------------------------
# 整合原语、流水账
# ----------------------------------------------------------------------

W, P, R = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20


def _flow(i, asset, amount, frm, to, kind=AssetFlowKind.ERC20):
    return AssetFlow(i, kind, asset, amount, frm, to, FlowSource.LOG, log_index=i)


def test_net_by_asset_and_trade_legs():
    flows = [
        _flow(0, "usdt", 100, W, R),
        _flow(1, "mid", 5, P, W),  # 中间 token：先进后出
        _flow(2, "mid", 5, W, P),
        _flow(3, "cake", 7, P, W),
        AssetFlow(4, AssetFlowKind.GAS, NATIVE, 9, W, "gas", FlowSource.TX),
    ]
    legs = trade_from_net(net_by_asset(flows, W))
    assert [(p.asset, p.net_raw) for p in legs.spend] == [("usdt", -100)]
    assert [(p.asset, p.net_raw) for p in legs.receive] == [("cake", 7)]
    assert [(p.asset, p.flow_ids) for p in legs.netted_out] == [("mid", (1, 2))]


def test_ledger_infer_appends_with_evidence_position():
    ledger = FlowLedger([_flow(0, "x", 1, W, P)], internal_available=False)
    fid = ledger.infer(AssetFlowKind.NATIVE, NATIVE, 5, P, W, evidence_log_index=42)
    f = ledger.get(fid)
    assert (fid, f.source, f.log_index) == (1, FlowSource.INFERRED, 42)
    ledger.claim([fid], "a@1")
    with pytest.raises(DecodeConflictError):
        ledger.claim([fid], "b@1")
