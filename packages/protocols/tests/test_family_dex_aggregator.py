"""dex_aggregator 家族：聚合器交换按钱包净额归并成 trade；换成原生币时缺内部交易数据，告警而不猜。"""

from __future__ import annotations

import pytest
from _golden import load
from alpha_core.types import Chain
from alpha_protocols.decoding.models import Direction, EventSubtype, WarningCode
from alpha_protocols.runtime import decode_context, decode_tx


def _decode(case):
    s = load(f"bsc/dex_aggregator/{case}")
    return decode_tx(Chain.BSC, s.tx, s.receipt, s.subject, ctx=decode_context(Chain.BSC, tokens=s.tokens))


def _agg(d):
    return [e for e in d.events if e.family == "dex_aggregator"]


@pytest.mark.parametrize("case", ["swap_e5e8894b", "swap_810c705b", "swap_a03de6a9"])
def test_token_to_token_swap_has_one_leg_each_way(case):
    d = _decode(case)
    events = _agg(d)
    assert sorted(e.event_subtype for e in events) == [EventSubtype.RECEIVE, EventSubtype.SPEND]
    assert all(
        e.direction is (Direction.OUT if e.event_subtype is EventSubtype.SPEND else Direction.IN) for e in events
    )
    assert not d.warnings


@pytest.mark.parametrize("case", ["swap_dad12b6c", "to_native"])
def test_swap_to_native_warns_and_marks_incomplete(case):
    """换成原生币：聚合器怎么把原生币给钱包不可知，不推断（规划 5.5），只告警并标记付出腿不完整。"""
    d = _decode(case)
    [spend] = _agg(d)
    assert spend.event_subtype is EventSubtype.SPEND and spend.extra["incomplete"] is True
    assert [w.code for w in d.warnings] == [WarningCode.INTERNAL_UNAVAILABLE]


def test_failed_aggregator_tx_only_has_gas():
    d = decode_tx(Chain.BSC, *[getattr(load("bsc/generic/failed"), k) for k in ("tx", "receipt", "subject")])
    assert _agg(d) == [] and [e.event_type.value for e in d.events] == ["fee"]


def test_two_sided_call_with_unregistered_method_is_still_a_trade():
    """callLiFi（0x849ce572）不在交换方法里，但两边都有资产流动：按交换归并。"""
    d = _decode("call_lifi_swap")
    events = _agg(d)
    assert sorted(e.event_subtype for e in events) == [EventSubtype.RECEIVE, EventSubtype.SPEND]
    assert all(e.extra == {"selector": "0x849ce572"} for e in events)


def test_one_sided_call_with_unregistered_method_is_not_guessed():
    """0x3ecba7f8 只有 USDT 转出（像跨链）：不认领、不猜语义，告警 unrecognized_call，资产流动由兜底记录。"""
    d = _decode("unrecognized_one_sided")
    assert _agg(d) == []
    assert WarningCode.UNRECOGNIZED_CALL in {w.code for w in d.warnings}
    assert any(e.event_type.value == "transfer" for e in d.events)
