"""第一段通用解码（T0）：用三条链的 64 个金标准样本验证资产流动、gas 模型、系统交易、风险标记和兜底。"""

from __future__ import annotations

import pytest
from _golden import all_case_ids, load
from alpha_protocols.decoding.evm.flows import TRANSFER, extract
from alpha_protocols.decoding.evm.pipeline import decode_evm_tx
from alpha_protocols.decoding.models import (
    NATIVE,
    AssetFlowKind,
    Direction,
    EventSubtype,
    EventType,
    FlowSource,
    WarningCode,
    leaf_flows,
)

ALL = all_case_ids()

# 只靠通用解码对不上余额的样本：钱包收到的原生币来自内部调用，要等家族的推断钩子（步骤 5 以后）
# 或数据源的内部交易补上。每个样本的缺口都已用推断规则逐 wei 核对过（规划 5.5）。
# 这里固定下来：通用解码的行为变了、或出现新的缺口，测试都会失败。
NEEDS_INFERENCE = {
    "bsc/compound_v2_like/borrow_vbnb",
    "bsc/compound_v2_like/redeem_vbnb",
    "bsc/dex_aggregator/swap_dad12b6c",
    "bsc/dex_aggregator/to_native",
    "bsc/uniswap_v2_like/add_liquidity_eth",
    "bsc/uniswap_v2_like/remove_liquidity_eth",
    "bsc/uniswap_v2_like/swap_tokens_for_eth",
    "bsc/uniswap_v3_like/exit_multicall_unwrap",
    "bsc/uniswap_v3_like/mint_native",
    "bsc/wrapped_native/unwrap",
    "ethereum/uniswap_v2_like/remove_liquidity_eth",
    "ethereum/uniswap_v2_like/swap_tokens_for_eth",
    "ethereum/uniswap_v2_like/swap_tokens_for_eth_tax_token",
    "ethereum/uniswap_v3_like/exit_multicall_unwrap",
    "ethereum/wrapped_native/unwrap",
    "base/uniswap_v2_like/remove_liquidity_eth",
    "base/uniswap_v2_like/swap_tokens_for_eth",
    "base/uniswap_v3_like/exit_multicall_unwrap",
    "base/wrapped_native/unwrap",
}


def _native_net(flows, subject: str) -> int:
    """钱包的原生币净额；只算叶子流水（被拆分的父流水不重复计算）。"""
    net = 0
    for f in leaf_flows(flows):
        if f.asset != NATIVE:
            continue
        if f.to_address == subject:
            net += f.amount_raw
        if f.from_address == subject:
            net -= f.amount_raw
    return net


def test_sample_set_is_complete():
    assert len(ALL) == 64
    assert NEEDS_INFERENCE <= set(ALL)


@pytest.mark.parametrize("case_id", ALL)
def test_native_flows_reconcile_with_balance_delta(case_id):
    """原生币流水（value、gas、L1 数据费、系统铸币）的净额必须逐 wei 等于链上余额差。"""
    s = load(case_id)
    assert s.attributable
    net = _native_net(extract(s.tx, s.receipt, s.subject, s.rules).flows, s.subject)
    if case_id in NEEDS_INFERENCE:
        assert net != s.balance_delta, "缺口消失了：更新 NEEDS_INFERENCE"
    else:
        assert net == s.balance_delta


@pytest.mark.parametrize("case_id", ALL)
def test_every_wallet_erc20_transfer_log_becomes_one_flow(case_id):
    s = load(case_id)
    wallet_topic = "0x" + "0" * 24 + s.subject[2:]
    logs = [
        lg
        for lg in s.receipt.logs
        if lg.topics and lg.topics[0] == TRANSFER and len(lg.topics) == 3 and wallet_topic in lg.topics[1:]
    ]
    flows = [f for f in extract(s.tx, s.receipt, s.subject, s.rules).flows if f.kind is AssetFlowKind.ERC20]
    assert sorted(f.log_index for f in flows) == sorted(lg.log_index for lg in logs)


@pytest.mark.parametrize("case_id", ALL)
def test_decode_is_deterministic_and_obeys_taxonomy(case_id):
    """全部样本都能解码（finalize 会按分类表校验每条事件），且重复解码结果完全相同。"""
    s = load(case_id)
    first = decode_evm_tx(s.tx, s.receipt, s.subject, s.context(), s.rules)
    second = decode_evm_tx(s.tx, s.receipt, s.subject, s.context(), s.rules)
    assert first == second
    assert [e.seq for e in first.events] == list(range(len(first.events)))
    claimed = {i for e in first.events for i in e.claimed_flow_ids}
    assert claimed == {f.flow_id for f in first.flows}, "兜底之后不能有未认领的流水"


def _events(case_id, **ctx):
    s = load(case_id)
    return s, decode_evm_tx(s.tx, s.receipt, s.subject, s.context(**ctx), s.rules)


def _kinds(decoded):
    return [(e.event_type, e.event_subtype, e.direction) for e in decoded.events]


def test_erc20_out_and_gas():
    s, d = _events("bsc/generic/erc20_out")
    assert _kinds(d) == [
        (EventType.TRANSFER, EventSubtype.NONE, Direction.OUT),
        (EventType.FEE, EventSubtype.NONE, Direction.OUT),
    ]
    transfer = d.events[0]
    log = next(lg for lg in s.receipt.logs if lg.topics[0] == TRANSFER)
    assert transfer.amount_raw == int(log.data, 16)
    assert transfer.counterparty_address == "0x" + log.topics[2][-40:]


def test_failed_tx_has_only_gas():
    _, d = _events("bsc/generic/failed")
    assert not d.succeeded
    assert _kinds(d) == [(EventType.FEE, EventSubtype.NONE, Direction.OUT)]


def test_approve_is_informational():
    _, d = _events("bsc/generic/approve")
    approve = [e for e in d.events if e.event_subtype is EventSubtype.APPROVE]
    assert len(approve) == 1 and approve[0].direction is Direction.NEUTRAL
    assert approve[0].claimed_flow_ids == () and "amount_raw" in approve[0].extra


@pytest.mark.parametrize("case_id", ["bsc/generic/impersonator_ascii", "bsc/generic/impersonator_confusable"])
def test_impersonator_tokens_are_spam_and_real_usdt_is_not(case_id):
    _, d = _events(case_id)
    usdt = "0x55d398326f99059ff775485246999027b3197955"
    fake = [e for e in d.events if e.asset != usdt and e.claimed_flow_ids]
    assert fake and all(
        (e.event_subtype, e.extra.get("risk_flag")) == (EventSubtype.SPAM, "impersonator") for e in fake
    )
    # 真 USDT 不会因为同一笔交易里有仿冒币就被标成仿冒；它若是 spam，只能是金额为 0 的投毒转账
    for e in d.events:
        if e.asset == usdt and e.event_subtype is EventSubtype.SPAM:
            assert e.extra == {"reason": "zero_transfer_from"} and e.amount_raw == 0


def test_approval_in_tx_not_sent_by_wallet_is_marked():
    """投毒交易里 transferFrom 顺带发出的 Approval（额度改成 0），不是钱包自己的授权。"""
    _, d = _events("bsc/generic/impersonator_ascii")
    [approve] = [e for e in d.events if e.event_subtype is EventSubtype.APPROVE]
    assert approve.extra["initiated_by_subject"] is False
    _, own = _events("bsc/generic/approve")
    assert all(e.extra["initiated_by_subject"] for e in own.events if e.event_subtype is EventSubtype.APPROVE)


def test_zero_transfer_from_is_poisoning():
    """别人发起的交易里，用真 token（Binance-Peg ETH）伪造一笔"钱包转出 0"的记录。"""
    _, d = _events("bsc/generic/zero_transfer_from")
    [e] = [e for e in d.events if e.direction is Direction.OUT]
    assert (e.event_type, e.event_subtype, e.amount_raw) == (EventType.RECEIVE, EventSubtype.SPAM, 0)
    assert e.extra == {"reason": "zero_transfer_from"}


def test_spam_nft_airdrop():
    _, d = _events("bsc/generic/spam_nft")
    nft = [e for e in d.events if e.token_id is not None]
    assert nft and all((e.event_subtype, e.extra.get("risk_flag")) == (EventSubtype.SPAM, "spam") for e in nft)


def test_base_gas_includes_l1_fee():
    s, d = _events("base/generic/native_out")
    [fee] = [e for e in d.events if e.event_type is EventType.FEE]
    assert s.receipt.l1_fee and fee.amount_raw == s.receipt.gas_used * s.receipt.effective_gas_price + s.receipt.l1_fee


def test_base_l1_deposit_is_bridge_in_without_gas():
    s, d = _events("base/generic/l1_deposit")
    assert s.tx.tx_type == 0x7E and s.tx.mint
    [bridge] = [e for e in d.events if e.event_type is EventType.BRIDGE]
    assert (bridge.direction, bridge.amount_raw, bridge.asset) == (Direction.IN, s.tx.mint, NATIVE)
    assert not [e for e in d.events if e.event_type is EventType.FEE], "存款交易不付 gas"
    # 这笔存款的 value 是钱包转给自己：一进一出两条 transfer，数量相抵
    self_transfer = [e for e in d.events if e.event_type is EventType.TRANSFER]
    assert sorted(e.direction for e in self_transfer) == [Direction.IN, Direction.OUT]
    assert {e.amount_raw for e in self_transfer} == {s.tx.value}
    [flow] = [f for f in d.flows if f.flow_id in bridge.claimed_flow_ids]
    assert flow.source is FlowSource.SYSTEM


def test_internal_transfers_from_data_source_close_the_gap():
    """数据源给出内部交易时，通用解码就能闭合原生币对账（这里用样本自己的缺口构造一笔内部交易）。"""
    from alpha_protocols.decoding.models import InternalTransfer

    s = load("bsc/uniswap_v3_like/exit_multicall_unwrap")
    gap = s.balance_delta - _native_net(extract(s.tx, s.receipt, s.subject, s.rules).flows, s.subject)
    internal = [InternalTransfer(s.tx.to_address, s.subject, gap)]
    flows = extract(s.tx, s.receipt, s.subject, s.rules, internal).flows
    assert _native_net(flows, s.subject) == s.balance_delta
    assert any(f.source is FlowSource.INTERNAL for f in flows)


def test_unknown_contract_logs_decoded_with_signature_or_reported():
    s = load("bsc/generic/misc_checkin")
    emitter = s.tx.to_address
    logs = [lg for lg in s.receipt.logs if lg.address == emitter]
    assert logs, "样本里签到合约应当发出了日志"
    topic0 = logs[0].topics[0]

    plain = decode_evm_tx(s.tx, s.receipt, s.subject, s.context(), s.rules)
    assert emitter in plain.unknown_contracts
    assert any(w.code is WarningCode.ABI_MISSING for w in plain.warnings)

    # 候选签名按可信度排序，对不上 topic0 的跳过
    ctx = s.context(event_signatures={topic0: ["Wrong(uint256)", "UserCheckedIn(address)"]})
    decoded = decode_evm_tx(s.tx, s.receipt, s.subject, ctx, s.rules)
    assert not decoded.warnings
    logs_events = [e for e in decoded.events if e.event_subtype is EventSubtype.DECODED_LOG]
    assert logs_events and logs_events[0].extra["event"] == "UserCheckedIn"
    assert logs_events[0].extra["abi_source"] == "signature_guess"
    assert logs_events[0].extra["args"]["arg0"] == s.subject


def test_contract_identified_as_unknown_is_still_reported_as_unknown():
    """识别第一层把查不出协议的合约记为 unknown：它仍然是未知合约，要报出来并尝试 ABI 解码。"""
    from alpha_protocols.decoding.context import ContractIdentity

    s = load("bsc/generic/misc_checkin")
    emitter = s.tx.to_address
    ctx = s.context(identities={emitter: ContractIdentity(emitter, "unknown")})
    d = decode_evm_tx(s.tx, s.receipt, s.subject, ctx, s.rules)
    assert emitter in d.unknown_contracts


def test_target_logs_of_third_party_tx_are_out_of_scope():
    """别人发起的交易里，交易目标发出、但没提到钱包的日志不属于钱包的交互（批量打款合约一笔上千条）。"""
    s = load("bsc/generic/misc_checkin")
    emitter = s.tx.to_address
    own = decode_evm_tx(s.tx, s.receipt, s.subject, s.context(), s.rules)
    assert emitter in own.unknown_contracts
    # 换一个既不是发起人、日志里也没提到的钱包视角：同一笔交易的目标日志不再算它的交互
    bystander = "0x" + "1" * 40
    assert all("0x" + "0" * 24 + bystander[2:] not in lg.topics for lg in s.receipt.logs)
    other = decode_evm_tx(s.tx, s.receipt, bystander, s.context(), s.rules)
    assert emitter not in other.unknown_contracts
    assert not any(w.code is WarningCode.ABI_MISSING for w in other.warnings)
