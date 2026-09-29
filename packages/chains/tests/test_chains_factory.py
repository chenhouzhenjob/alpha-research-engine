"""按链构造适配器：环境变量前缀、链规格默认值、BSC 旧入口行为不变。"""

from __future__ import annotations

import pytest
from alpha_chains.bsc import build_bsc_adapter
from alpha_chains.factory import build_evm_adapter, rpc_urls_env
from alpha_core.types import CHAIN_SPECS, EVM_CHAIN_IDS, Chain


def test_every_chain_has_spec_and_chain_id():
    assert set(CHAIN_SPECS) == set(Chain)
    assert EVM_CHAIN_IDS == {Chain.BSC: 56, Chain.ETHEREUM: 1, Chain.BASE: 8453}


def test_env_prefix_per_chain(monkeypatch):
    monkeypatch.setenv("BASE_RPC_URLS", "https://a.example, https://b.example")
    monkeypatch.setenv("BASE_LOG_CHUNK_SIZE", "7000")
    adapter = build_evm_adapter(Chain.BASE)
    assert adapter.chain == Chain.BASE
    assert [e.url for e in adapter._endpoints] == ["https://a.example", "https://b.example"]
    assert adapter._log_chunk_size == 7000
    assert rpc_urls_env(Chain.ETHEREUM) == "ETH_RPC_URLS"


def test_missing_rpc_env_raises(monkeypatch):
    monkeypatch.delenv("ETH_RPC_URLS", raising=False)
    with pytest.raises(ValueError, match="ETH_RPC_URLS"):
        build_evm_adapter(Chain.ETHEREUM)


def test_bsc_entry_keeps_bnb_env_and_default_chunk(monkeypatch):
    monkeypatch.setenv("BNB_RPC_URLS", "https://bsc.example")
    monkeypatch.delenv("BNB_LOG_CHUNK_SIZE", raising=False)
    adapter = build_bsc_adapter()
    assert adapter.chain == Chain.BSC
    assert adapter._log_chunk_size == 45_000
