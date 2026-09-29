"""统一调用路径：故障转移、429 判定、瞬时重试、记账、批量、区块时间持久化缓存。

用假的端点 `post` 代替真实网络，回执结构来自真实 BSC 节点录制的夹具。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
from alpha_chains import evm_common
from alpha_chains.evm_common import EvmChainAdapter, RpcResponseError
from alpha_chains.rate_limit import CuTokenBucket
from alpha_core.errors import ChainAdapterError, RpcQuotaExhaustedError
from alpha_core.metering import CallStatus, InMemoryCallMeter
from alpha_core.ports import BlockTimeSource
from alpha_core.types import Chain

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "bsc_tx_sample.json").read_text())
NODEREAL = "https://bsc-mainnet.nodereal.io/v1/key"
PUBLIC = "https://bsc-rpc.publicnode.com"


def _adapter(urls, **kwargs):
    meter = InMemoryCallMeter(app="test")
    adapter = EvmChainAdapter(Chain.BSC, urls, meter=meter, sleep=lambda _s: None, **kwargs)
    return adapter, meter


def _totals(meter):
    return {(k.provider, k.method, k.status): v for k, v in meter.snapshot().items()}


def _responder(handler):
    """把 handler(payload) 包装成端点的 post；handler 对单个请求返回 result 或抛内部异常。"""

    def post(payload):
        if isinstance(payload, list):
            return [{"jsonrpc": "2.0", "id": p["id"], **handler(p)} for p in payload]
        return {"jsonrpc": "2.0", "id": payload["id"], **handler(payload)}

    return post


def test_single_rpc_success_is_metered_with_cu():
    adapter, meter = _adapter([NODEREAL])
    adapter._endpoints[0].post = _responder(lambda p: {"result": "0x5"})
    assert adapter.get_transaction_count("0xABC") == 5
    t = _totals(meter)[("nodereal", "eth_getTransactionCount", CallStatus.OK)]
    assert (t.call_count, t.est_cu) == (1, 25)


def test_quota_on_first_endpoint_fails_over_to_second():
    adapter, meter = _adapter([NODEREAL, PUBLIC])

    def quota(_payload):
        raise evm_common._QuotaError("HTTP 429")

    adapter._endpoints[0].post = quota
    adapter._endpoints[1].post = _responder(lambda p: {"result": "0x1"})
    assert adapter.get_transaction_count("0xabc") == 1
    t = _totals(meter)
    assert t[("nodereal", "eth_getTransactionCount", CallStatus.RATE_LIMITED)].call_count == 1
    assert t[("publicnode", "eth_getTransactionCount", CallStatus.OK)].est_cu == 0


def test_all_endpoints_quota_raises_without_retrying_same_endpoint():
    adapter, meter = _adapter([NODEREAL, PUBLIC])
    calls = []

    def quota(payload):
        calls.append(payload)
        raise evm_common._QuotaError("HTTP 429")

    for e in adapter._endpoints:
        e.post = quota
    with pytest.raises(RpcQuotaExhaustedError):
        adapter.get_transaction_count("0xabc")
    assert len(calls) == 2  # 每个端点只试一次，不原地重试
    assert isinstance(RpcQuotaExhaustedError("x"), ChainAdapterError)


def test_jsonrpc_quota_message_is_treated_as_quota():
    adapter, _ = _adapter([NODEREAL])
    adapter._endpoints[0].post = _responder(lambda p: {"error": {"code": -32005, "message": "Monthly quota exceeded"}})
    with pytest.raises(RpcQuotaExhaustedError):
        adapter.get_transaction_count("0xabc")


def test_transient_errors_retry_on_same_endpoint():
    adapter, meter = _adapter([NODEREAL], transient_attempts=3)
    attempts = []

    def flaky(payload):
        attempts.append(1)
        if len(attempts) < 3:
            raise evm_common._TransientError("timeout")
        return _responder(lambda p: {"result": "0x2"})(payload)

    adapter._endpoints[0].post = flaky
    assert adapter.get_transaction_count("0xabc") == 2
    t = _totals(meter)
    assert t[("nodereal", "eth_getTransactionCount", CallStatus.ERROR)].call_count == 2
    assert t[("nodereal", "eth_getTransactionCount", CallStatus.OK)].call_count == 1


def test_deterministic_rpc_error_does_not_fail_over():
    adapter, _ = _adapter([NODEREAL, PUBLIC])
    adapter._endpoints[0].post = _responder(lambda p: {"error": {"code": -32000, "message": "execution reverted"}})
    second = []
    adapter._endpoints[1].post = lambda p: second.append(p)
    with pytest.raises(RpcResponseError) as exc_info:
        adapter.get_storage_at("0xabc", 0)
    assert exc_info.value.message == "execution reverted"
    assert second == []


def test_receipts_batch_parses_real_receipt_and_reports_missing():
    adapter, meter = _adapter([NODEREAL], batch_size=10)
    real = FIXTURE["receipt"]
    missing = "0x" + "11" * 32

    def handler(p):
        return {"result": real if p["params"][0] == real["transactionHash"] else None}

    adapter._endpoints[0].post = _responder(handler)
    result = adapter.get_transaction_receipts([real["transactionHash"].upper().replace("0X", "0x"), missing])
    receipt = result.ok[real["transactionHash"].lower()]
    assert receipt.block_number == int(real["blockNumber"], 16)
    assert len(receipt.logs) == len(real["logs"])
    assert all(t.startswith("0x") and t == t.lower() for log in receipt.logs for t in log.topics)
    assert receipt.logs[0].log_index == int(real["logs"][0]["logIndex"], 16)
    assert missing in result.failed
    assert _totals(meter)[("nodereal", "eth_getTransactionReceipt", CallStatus.OK)].est_cu == 30


def test_batch_item_error_is_retried_individually_then_reported():
    adapter, _ = _adapter([NODEREAL])
    seen: dict[str, int] = {}

    def handler(p):
        addr = p["params"][0]
        seen[addr] = seen.get(addr, 0) + 1
        if addr == "0xbad":
            return {"error": {"code": -32000, "message": "boom"}}
        return {"result": "0x6080"}

    adapter._endpoints[0].post = _responder(handler)
    result = adapter.get_codes(["0xGood".lower(), "0xBAD"])
    assert result.ok == {"0xgood": "0x6080"}
    assert "boom" in result.failed["0xbad"]
    assert seen["0xbad"] == 2  # 批量一次 + 单独重试一次


def test_transaction_parse_and_selector():
    adapter, _ = _adapter([NODEREAL])
    tx = FIXTURE["tx"]
    adapter._endpoints[0].post = _responder(lambda p: {"result": tx})
    info = adapter.get_transactions([tx["hash"]]).ok[tx["hash"].lower()]
    assert info.method_selector == tx["input"][:10].lower()
    assert info.value == int(tx["value"], 16)


class _MemStore:
    def __init__(self, preset=None):
        self.data = dict(preset or {})
        self.puts = []

    def get_many(self, chain, block_numbers):
        return {b: self.data[b] for b in block_numbers if b in self.data}

    def put_many(self, chain, times, source):
        self.puts.append((dict(times), source))
        self.data.update(times)


def test_block_timestamps_use_store_before_rpc_and_write_back():
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    store = _MemStore({100: t1})
    adapter, _ = _adapter([NODEREAL], block_time_store=store)
    requested = []

    def handler(p):
        requested.append(int(p["params"][0], 16))
        return {"result": {"timestamp": hex(1_800_000_000)}}

    adapter._endpoints[0].post = _responder(handler)
    result = adapter.get_block_timestamps([100, 101])
    assert result.ok[100] == t1
    assert result.ok[101] == datetime.fromtimestamp(1_800_000_000, tz=UTC)
    assert requested == [101]
    assert store.puts == [({101: result.ok[101]}, BlockTimeSource.RPC)]
    # 第二次全部命中进程内存，不再请求
    adapter.get_block_timestamps([100, 101])
    assert requested == [101]


class _HttpErr(requests.exceptions.HTTPError):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status})()


class _FakeEth:
    def __init__(self, exc=None, logs=None):
        self.exc = exc
        self.logs = logs or []
        self.params = None

    @property
    def block_number(self):
        if self.exc:
            raise self.exc
        return 7

    def get_logs(self, params):
        self.params = params
        return self.logs


def test_legacy_path_all_429_raises_quota_error_and_meters():
    adapter, meter = _adapter([NODEREAL, PUBLIC])
    for e in adapter._endpoints:
        e.web3 = type("W", (), {"eth": _FakeEth(exc=_HttpErr(429))})()
    with pytest.raises(RpcQuotaExhaustedError):
        adapter._with_failover(lambda c: c.eth.block_number, "eth_blockNumber")
    assert _totals(meter)[("nodereal", "eth_blockNumber", CallStatus.RATE_LIMITED)].call_count == 1


def test_legacy_get_logs_accepts_address_list_and_or_topics():
    adapter, meter = _adapter([NODEREAL], log_chunk_size=1000)
    eth = _FakeEth()
    adapter._endpoints[0].web3 = type("W", (), {"eth": eth})()
    adapter.get_logs(
        address=["0x55d398326f99059ff775485246999027b3197955", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"],
        topics=["0xddf2", None, ["0xaa", "0xbb"]],
        from_block=1,
        to_block=1500,
    )
    assert isinstance(eth.params["address"], list) and eth.params["address"][0].startswith("0x55d3")
    assert eth.params["topics"][2] == ["0xaa", "0xbb"]
    assert _totals(meter)[("nodereal", "eth_getLogs", CallStatus.OK)].call_count == 2  # 1500 块按 1000 分两段


def test_rate_limiter_is_applied_before_requests():
    waits = []
    clock = [0.0]

    def sleep(s):
        waits.append(s)
        clock[0] += s

    bucket = CuTokenBucket(30, clock=lambda: clock[0], sleep=sleep)
    adapter, _ = _adapter([NODEREAL], rate_limiter=bucket)
    adapter._endpoints[0].post = _responder(lambda p: {"result": "0x1"})
    adapter.get_transaction_count("0xabc")  # 25 CU，桶里有 30
    adapter.get_transaction_count("0xabc")  # 还剩 5，需要等 20/30 秒
    assert waits == [pytest.approx(20 / 30)]


def test_block_ref_is_passed_as_hex_and_latest_by_default():
    adapter, _meter = _adapter([PUBLIC])
    seen = []

    def handler(p):
        seen.append((p["method"], p["params"][-1]))
        return {"result": "0x02"}

    adapter._endpoints[0].post = _responder(handler)
    adapter.get_transaction_count("0xabc")
    adapter.get_transaction_count("0xabc", block=40_000_000)
    adapter.get_storage_at("0xabc", 1, block=7)
    adapter.raw_call(to="0xabc", data="0x", block=8)
    assert seen == [
        ("eth_getTransactionCount", "latest"),
        ("eth_getTransactionCount", "0x2625a00"),
        ("eth_getStorageAt", "0x7"),
        ("eth_call", "0x8"),
    ]


def test_block_ref_rejects_invalid_values():
    adapter, _meter = _adapter([PUBLIC])
    for bad in (-1, "earliest", True):
        with pytest.raises(ValueError):
            adapter.get_transaction_count("0xabc", block=bad)


def test_get_balances_batches_and_reports_failures():
    adapter, meter = _adapter([PUBLIC])

    def handler(p):
        addr, tag = p["params"]
        assert tag == "0x10"
        if addr == "0xbad":
            return {"error": {"code": -32000, "message": "missing trie node"}}
        return {"result": "0xde0b6b3a7640000"}

    adapter._endpoints[0].post = _responder(handler)
    result = adapter.get_balances(["0xAAA", "0xaaa", "0xbad"], block=16)
    assert result.ok == {"0xaaa": 10**18}
    assert "missing trie node" in result.failed["0xbad"]
    assert _totals(meter)[("publicnode", "eth_getBalance", CallStatus.OK)].call_count == 1


BASE_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "base_op_stack_sample.json").read_text())


def test_op_stack_fields_are_parsed_and_absent_elsewhere():
    """Base 真实录制：普通交易回执带 l1Fee，存款交易（类型 0x7e）带 mint；BSC 样本这些字段为 None。"""
    adapter, _ = _adapter([PUBLIC])
    by_hash = {
        BASE_FIXTURE["tx"]["hash"]: (BASE_FIXTURE["tx"], BASE_FIXTURE["receipt"]),
        BASE_FIXTURE["deposit_tx"]["hash"]: (BASE_FIXTURE["deposit_tx"], BASE_FIXTURE["deposit_receipt"]),
        FIXTURE["tx"]["hash"]: (FIXTURE["tx"], FIXTURE["receipt"]),
    }

    def handler(p):
        tx, rc = by_hash[p["params"][0]]
        return {"result": tx if p["method"] == "eth_getTransactionByHash" else rc}

    adapter._endpoints[0].post = _responder(handler)
    hashes = [h.lower() for h in by_hash]
    txs = adapter.get_transactions(hashes).ok
    receipts = adapter.get_transaction_receipts(hashes).ok
    normal, deposit, bsc = hashes
    assert receipts[normal].l1_fee == int(BASE_FIXTURE["receipt"]["l1Fee"], 16) > 0
    assert (txs[deposit].tx_type, txs[deposit].mint) == (0x7E, int(BASE_FIXTURE["deposit_tx"]["mint"], 16))
    assert txs[normal].tx_type == 2 and txs[normal].mint is None
    assert receipts[bsc].l1_fee is None and txs[bsc].mint is None
