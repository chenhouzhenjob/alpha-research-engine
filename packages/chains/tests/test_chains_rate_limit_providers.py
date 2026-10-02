import pytest
from alpha_chains.providers import (
    PROVIDER_ANKR,
    PROVIDER_NODEREAL,
    PROVIDER_PUBLICNODE,
    PROVIDER_UNKNOWN,
    cu_for,
    cu_for_rate_limit,
    detect_provider,
)
from alpha_chains.rate_limit import CuTokenBucket


def test_detect_provider_by_host():
    assert detect_provider("https://bsc-mainnet.nodereal.io/v1/abc") == PROVIDER_NODEREAL
    assert detect_provider("https://bsc-rpc.publicnode.com") == PROVIDER_PUBLICNODE
    assert detect_provider("https://rpc.ankr.com/bsc/xyz") == PROVIDER_ANKR
    assert detect_provider("https://example.org") == PROVIDER_UNKNOWN


def test_cu_lookup():
    assert cu_for(PROVIDER_NODEREAL, "eth_getTransactionReceipt") == 15
    assert cu_for(PROVIDER_PUBLICNODE, "eth_getLogs") == 0
    assert cu_for(PROVIDER_NODEREAL, "eth_somethingNew") is None
    assert cu_for(PROVIDER_UNKNOWN, "eth_call") is None
    assert cu_for_rate_limit(PROVIDER_NODEREAL, "eth_somethingNew") == 20


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_bucket_disabled_never_waits():
    assert CuTokenBucket(None).acquire(10_000) == 0.0


def test_bucket_waits_for_refill():
    c = _Clock()
    b = CuTokenBucket(100, clock=c.now, sleep=c.sleep)
    assert b.acquire(100) == 0.0
    assert b.acquire(50) == pytest.approx(0.5)


def test_bucket_oversized_request_waits_until_full_then_goes_negative():
    c = _Clock()
    b = CuTokenBucket(100, clock=c.now, sleep=c.sleep)
    b.acquire(100)
    assert b.acquire(250) == pytest.approx(1.0)  # 等到桶满（100）就放行，余额变成 -150
    assert b.acquire(10) == pytest.approx(1.6)  # 需要从 -150 补回到 10


def test_bucket_rejects_non_positive_capacity():
    with pytest.raises(ValueError):
        CuTokenBucket(0)
