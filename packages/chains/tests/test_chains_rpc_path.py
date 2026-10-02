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
from alpha_core.errors import ChainAdapterError, RpcQuotaExhaustedError, RpcRateLimitedError
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
    assert t[("nodereal", "eth_getTransactionCount", CallStatus.QUOTA_EXHAUSTED)].call_count == 1
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
    adapter._endpoints[0].post = _responder(
        lambda p: {"error": {"code": -32005, "message": "You've reached your monthly quota limit."}}
    )
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
    def __init__(self, status, text=""):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status, "text": text, "headers": {}})()


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


def test_legacy_path_all_quota_429_raises_quota_error_and_meters():
    adapter, meter = _adapter([NODEREAL, NODEREAL.replace("key", "key2")])
    for e in adapter._endpoints:
        e.web3 = type("W", (), {"eth": _FakeEth(exc=_HttpErr(429, "You've reached your monthly quota limit."))})()
    with pytest.raises(RpcQuotaExhaustedError):
        adapter._with_failover(lambda c: c.eth.block_number, "eth_blockNumber")
    assert _totals(meter)[("nodereal", "eth_blockNumber", CallStatus.QUOTA_EXHAUSTED)].call_count == 2


def test_legacy_path_plain_429_is_rate_limit_not_quota():
    """web3 自己对 429 重试放弃之后：没有额度原文的 429 是短时限速，不能当成额度耗尽让任务停到下个周期。"""
    adapter, meter = _adapter([NODEREAL, PUBLIC])
    for e in adapter._endpoints:
        e.web3 = type("W", (), {"eth": _FakeEth(exc=_HttpErr(429, "Too Many Requests"))})()
    with pytest.raises(RpcRateLimitedError) as info:
        adapter._with_failover(lambda c: c.eth.block_number, "eth_blockNumber")
    assert not isinstance(info.value, RpcQuotaExhaustedError)
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


# ----------------------------------------------------------------------
# 429 分两类：短时限速（同一端点退避重试）与计划额度用完（不重试、切换端点）
# ----------------------------------------------------------------------

ANKR = "https://rpc.ankr.com/bsc/key"
ANKR_RATE_LIMIT = (
    '[{"id":1,"jsonrpc":"2.0","error":{"code":-32090,'
    '"message":"Too many requests, reason: call rate limit exhausted, retry in 10s"}}]'
)
NODEREAL_MONTHLY = (
    '{"jsonrpc":"2.0","id":null,"error":{"code":-32005,"message":"You\'ve reached your monthly quota limit."}}'
)


class _Resp:
    def __init__(self, status, text="", headers=None, body=None):
        self.status_code, self.text, self.headers = status, text, headers or {}
        self._body = body

    def json(self):
        return self._body


def _http(adapter, index, responses):
    """让第 index 个端点的 HTTP 会话依次返回给定响应（最后一个重复使用），走真实的 _Endpoint.post。"""
    queue = list(responses)
    calls = []

    def post(url, json, timeout):
        calls.append(json)
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        if resp.status_code == 200 and resp._body is None:
            payload = json
            resp = _Resp(200, body={"jsonrpc": "2.0", "id": payload["id"], "result": "0x7"})
        return resp

    adapter._endpoints[index].session.post = post
    return calls


def _limited_adapter(urls, **kwargs):
    sleeps = []
    meter = InMemoryCallMeter(app="test")
    adapter = EvmChainAdapter(Chain.BSC, urls, meter=meter, sleep=sleeps.append, **kwargs)
    return adapter, meter, sleeps


def test_ankr_rate_limit_retries_same_endpoint_after_hinted_wait():
    adapter, meter, sleeps = _limited_adapter([ANKR, PUBLIC])
    ankr_calls = _http(adapter, 0, [_Resp(429, ANKR_RATE_LIMIT), _Resp(200)])
    public_calls = _http(adapter, 1, [_Resp(200)])
    assert adapter.get_transaction_count("0xabc") == 7
    assert sleeps == [10.0] and len(ankr_calls) == 2 and public_calls == []
    t = _totals(meter)
    assert t[("ankr", "eth_getTransactionCount", CallStatus.RATE_LIMITED)].call_count == 1
    assert t[("ankr", "eth_getTransactionCount", CallStatus.OK)].call_count == 1


def test_retry_after_header_and_wait_cap():
    adapter, _, sleeps = _limited_adapter([PUBLIC], max_rate_limit_wait=5.0)
    _http(adapter, 0, [_Resp(429, "Too Many Requests", {"Retry-After": "3"}), _Resp(429, "retry in 60s"), _Resp(200)])
    adapter.get_transaction_count("0xabc")
    assert sleeps == [3.0, 5.0]  # 第二次建议 60 秒，封顶 5 秒


def test_rate_limit_exhausted_fails_over_then_all_limited_raises_rate_limited():
    adapter, meter, sleeps = _limited_adapter([ANKR, PUBLIC], rate_limit_attempts=2)
    ankr_calls = _http(adapter, 0, [_Resp(429, ANKR_RATE_LIMIT)])
    _http(adapter, 1, [_Resp(200)])
    assert adapter.get_transaction_count("0xabc") == 7
    assert len(ankr_calls) == 3 and len(sleeps) == 2  # 原端点 1 次 + 重试 2 次，用尽后切换

    adapter, meter, sleeps = _limited_adapter([ANKR, PUBLIC], rate_limit_attempts=1)
    _http(adapter, 0, [_Resp(429, ANKR_RATE_LIMIT)])
    _http(adapter, 1, [_Resp(429, "Too Many Requests")])
    with pytest.raises(RpcRateLimitedError) as info:
        adapter.get_transaction_count("0xabc")
    assert not isinstance(info.value, RpcQuotaExhaustedError)
    assert sleeps == [10.0, 1.0]  # Ankr 按原文 10 秒；PublicNode 没给提示，按 1 秒起退避


def test_monthly_quota_does_not_retry_and_all_quota_raises_quota_error():
    nodereal2 = NODEREAL.replace("key", "key2")
    adapter, meter, sleeps = _limited_adapter([NODEREAL, nodereal2])
    first = _http(adapter, 0, [_Resp(429, NODEREAL_MONTHLY)])
    _http(adapter, 1, [_Resp(429, NODEREAL_MONTHLY)])
    with pytest.raises(RpcQuotaExhaustedError):
        adapter.get_transaction_count("0xabc")
    assert sleeps == [] and len(first) == 1, "额度耗尽不原地重试"
    assert _totals(meter)[("nodereal", "eth_getTransactionCount", CallStatus.QUOTA_EXHAUSTED)].call_count == 2


def test_quota_on_one_endpoint_and_rate_limit_on_other_raises_rate_limited():
    """还有端点只是限速：稍后重试就能恢复，不能按额度耗尽让任务停到下个计费周期。"""
    adapter, _, _ = _limited_adapter([NODEREAL, ANKR], rate_limit_attempts=0)
    _http(adapter, 0, [_Resp(429, NODEREAL_MONTHLY)])
    _http(adapter, 1, [_Resp(429, ANKR_RATE_LIMIT)])
    with pytest.raises(RpcRateLimitedError):
        adapter.get_transaction_count("0xabc")


def test_classify_limit_by_vendor_text():
    from alpha_chains.providers import LimitKind, classify_limit

    def kind(provider, text, status=429):
        signal = classify_limit(provider, text, http_status=status)
        return None if signal is None else (signal.kind, signal.retry_after)

    assert kind("nodereal", "You've reached your monthly quota limit.") == (LimitKind.QUOTA_EXHAUSTED, None)
    assert kind("nodereal", "You have reached the maximum CUPS limit") == (LimitKind.RATE_LIMITED, None)  # 每秒 CU
    assert kind("ankr", "call rate limit exhausted, retry in 10s") == (LimitKind.RATE_LIMITED, 10.0)
    assert kind("ankr", "You have reached the maximum allowed number of requests") == (LimitKind.QUOTA_EXHAUSTED, None)
    assert kind("unknown", "quota exceeded") == (LimitKind.RATE_LIMITED, None)  # 认不出厂商时只有 monthly 才算额度
    assert kind("unknown", "monthly capacity limit exceeded") == (LimitKind.QUOTA_EXHAUSTED, None)
    assert kind("publicnode", "monthly limit") == (LimitKind.RATE_LIMITED, None)  # 免费公共节点没有计划额度
    assert kind("ankr", "retry in 250ms")[1] == 0.25
    assert kind("nodereal", "execution reverted", status=200) is None  # 不是限流类错误
    assert kind("nodereal", "rate limit exceeded", status=200) == (LimitKind.RATE_LIMITED, None)  # 200 正文里的限流
