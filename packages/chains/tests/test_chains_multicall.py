"""Multicall3 编码解码、整批失败拆分、子调用失败上报，以及 ERC20 批量辅助函数。"""

from __future__ import annotations

import pytest
from alpha_chains.erc20 import read_balances, read_metadata_batch
from alpha_chains.evm_common import RpcResponseError
from alpha_chains.multicall import MULTICALL3_ADDRESS, Call, get_eth_balances, multicall
from alpha_core.errors import RpcQuotaExhaustedError
from eth_abi import decode, encode

USDT = "0x55d398326f99059ff775485246999027b3197955"
WALLET = "0x05bbf9032f4c829e31a1f1b0b725d77329fad6be"


class FakeChain:
    """模拟 Multicall3：按子调用的 (target, 选择器) 查表返回；可以设置"超过 N 个子调用就整批失败"。"""

    def __init__(self, table, *, max_calls=None, quota=False):
        self.table = table
        self.max_calls = max_calls
        self.quota = quota
        self.batches: list[int] = []

    def raw_call(self, *, to: str, data: str, block="latest") -> bytes:
        assert to == MULTICALL3_ADDRESS
        raw = bytes.fromhex(data[2:])
        assert raw[:4].hex() == "bce38bd7"
        require_success, calls = decode(["bool", "(address,bytes)[]"], raw[4:])
        assert require_success is False
        self.batches.append(len(calls))
        if self.quota:
            raise RpcQuotaExhaustedError("quota")
        if self.max_calls is not None and len(calls) > self.max_calls:
            raise RpcResponseError("eth_call", -32000, "out of gas")
        out = []
        for target, cd in calls:
            key = (target.lower(), cd[:4].hex())
            if key in self.table:
                value = self.table[key]
                out.append((True, value(cd) if callable(value) else value))
            else:
                out.append((False, b""))
        return encode(["(bool,bytes)[]"], [out])


def _uint(v):
    return encode(["uint256"], [v])


def test_multicall_preserves_order_and_reports_reverts():
    chain = FakeChain({(USDT, "313ce567"): _uint(18)})
    res = multicall(chain, [Call(USDT, bytes.fromhex("313ce567")), Call(USDT, bytes.fromhex("deadbeef"))])
    assert res[0].success and int.from_bytes(res[0].data, "big") == 18
    assert not res[1].success and res[1].error is None


def test_whole_batch_failure_is_split_in_half_until_it_fits():
    chain = FakeChain({(USDT, "313ce567"): _uint(18)}, max_calls=2)
    calls = [Call(USDT, bytes.fromhex("313ce567"))] * 5
    res = multicall(chain, calls, chunk_size=5)
    assert all(r.success for r in res)
    assert chain.batches == [5, 3, 2, 1, 2]  # 5 失败 → 3 失败 → 2、1 成功 → 2 成功


def test_single_call_that_still_fails_is_marked_with_error():
    chain = FakeChain({}, max_calls=0)
    (res,) = multicall(chain, [Call(USDT, b"\x00\x00\x00\x00")])
    assert not res.success and "out of gas" in res.error


def test_quota_is_not_split_but_raised():
    chain = FakeChain({}, quota=True)
    with pytest.raises(RpcQuotaExhaustedError):
        multicall(chain, [Call(USDT, b"\x00" * 4)] * 4, chunk_size=4)
    assert chain.batches == [4]


def test_eth_balances_via_multicall_itself():
    chain = FakeChain({(MULTICALL3_ADDRESS, "4d2301cc"): lambda cd: _uint(7 if cd[-20:].hex() == WALLET[2:] else 0)})
    res = get_eth_balances(chain, [WALLET.upper().replace("0X", "0x"), "0x" + "00" * 20])
    assert res.ok[WALLET] == 7
    assert res.ok["0x" + "00" * 20] == 0


def test_metadata_handles_string_and_bytes32_symbols():
    mkr_like = "0x" + "ab" * 20
    chain = FakeChain(
        {
            (USDT, "313ce567"): _uint(18),
            (USDT, "95d89b41"): encode(["string"], ["USDT"]),
            (USDT, "06fdde03"): encode(["string"], ["Tether USD"]),
            (mkr_like, "313ce567"): _uint(18),
            (mkr_like, "95d89b41"): b"MKR".ljust(32, b"\x00"),
        }
    )
    meta = read_metadata_batch(chain, [USDT, mkr_like])
    assert (meta[USDT].decimals, meta[USDT].symbol, meta[USDT].name) == (18, "USDT", "Tether USD")
    assert (meta[mkr_like].symbol, meta[mkr_like].name) == ("MKR", None)


def test_balances_report_failed_pairs():
    chain = FakeChain({(USDT, "70a08231"): _uint(123)})
    other = "0x" + "cd" * 20
    res = read_balances(chain, [(USDT, WALLET), (other, WALLET)])
    assert res.ok[(USDT, WALLET)] == 123
    assert (other, WALLET) in res.failed


def test_rate_limited_is_not_split_into_smaller_batches():
    """限速和批次大小无关：不能像普通失败那样对半拆分，否则调用次数成倍增加。"""
    from alpha_core.errors import RpcRateLimitedError

    calls = []

    class _Limited:
        def raw_call(self, *, to, data, block="latest"):
            calls.append(data)
            raise RpcRateLimitedError("限速")

    with pytest.raises(RpcRateLimitedError):
        multicall(_Limited(), [Call("0x" + "11" * 20, b"\x00")] * 8)
    assert len(calls) == 1
