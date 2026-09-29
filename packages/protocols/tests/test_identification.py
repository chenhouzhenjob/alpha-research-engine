"""识别第一层：实例角色、已有表、CREATE2 校验、字节码判断 EOA、注册表发现。

读取用假 reader 回放真实的链上返回值（token0/token1/fee 等），地址都是真实的池子和交易对。
"""

from __future__ import annotations

import json
from pathlib import Path

from alpha_core.ports import ContractRecord, ReviewStatus
from alpha_core.types import Chain
from alpha_protocols.identification.runner import discover_registries, identify
from eth_abi import encode

WBNB, USDT = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "0x55d398326f99059ff775485246999027b3197955"
V2_PAIR = (
    "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"  # PancakeSwap V2 WBNB/USDT（getPair 与 CREATE2 一致，2026-09-29 核实）
)
V3_POOL = "0x36696169c63e42cd08ce11f5deebbcebae652050"  # PancakeSwap V3 WBNB/USDT 0.05%
FAKE_POOL = "0x" + "77" * 20  # 返回同样 token 的冒牌合约：CREATE2 对不上
EOA = "0x" + "99" * 20
NPM = "0x46a15b0b27311cedf172ab29e4f4766fbe7f4364"
TOKEN0, TOKEN1, FEE = "0x0dfe1681", "0xd21220a7", "0xddca3f43"  # token0()、token1()、fee()


class _Store:
    def __init__(self, preset=()):
        self.rows = {r.address: r for r in preset}
        self.writes = 0

    def get_many(self, chain, addresses):
        return {a: self.rows[a] for a in addresses if a in self.rows}

    def upsert_many(self, records):
        self.writes += 1
        for r in records:
            if self.rows.get(r.address, r).review_status is not ReviewStatus.CONFIRMED:
                self.rows[r.address] = r


def _addr(a):
    return encode(["address"], [a])


STATE = {
    V2_PAIR: {TOKEN0: _addr(USDT), TOKEN1: _addr(WBNB)},  # V2 交易对没有 fee()，读出来是失败
    V3_POOL: {TOKEN0: _addr(USDT), TOKEN1: _addr(WBNB), FEE: encode(["uint24"], [500])},
    FAKE_POOL: {TOKEN0: _addr(USDT), TOKEN1: _addr(WBNB), FEE: encode(["uint24"], [500])},
}


def _reader(calls_log):
    def read(batch):
        calls_log.extend(batch)
        return {r.key: STATE.get(r.to, {}).get(r.data) for r in batch}

    return read


def _codes(addresses):
    return {a: "0x" if a == EOA else "0x6080604052" for a in addresses}


def test_identify_all_first_layer_methods():
    store, calls = _Store(), []
    got = identify(
        Chain.BSC, [V2_PAIR, V3_POOL, FAKE_POOL, EOA, NPM], store=store, reader=_reader(calls), code_reader=_codes
    )
    assert (got[NPM].kind, got[NPM].source, got[NPM].instance_key) == (
        "position_manager",
        "static_roles",
        "pancakeswap-v3",
    )
    assert (got[V2_PAIR].kind, got[V2_PAIR].source, got[V2_PAIR].instance_key) == ("pair", "create2", "pancakeswap-v2")
    assert (got[V3_POOL].kind, got[V3_POOL].instance_key) == ("pool", "pancakeswap-v3")
    assert got[V3_POOL].evidence["components"] == [USDT, WBNB, 500]
    assert (got[FAKE_POOL].kind, got[FAKE_POOL].family) == ("unknown", None) and got[FAKE_POOL].code_hash
    assert (got[EOA].kind, got[EOA].source) == ("eoa", "code")
    assert all(c.to != NPM for c in calls), "实例角色地址不需要读链"

    # 第二次识别：全部已有结果，不再读链
    calls.clear()
    identify(Chain.BSC, [V2_PAIR, EOA], store=store, reader=_reader(calls), code_reader=_codes)
    assert calls == []


def test_known_table_avoids_rpc():
    store, calls = _Store(), []
    got = identify(
        Chain.BSC,
        [V3_POOL],
        store=store,
        reader=_reader(calls),
        code_reader=_codes,
        known_components={("pancakeswap-v3", V3_POOL): (USDT, WBNB, 500)},
    )
    assert (got[V3_POOL].source, calls) == ("known_table", [])


def test_confirmed_record_is_not_overwritten():
    manual = ContractRecord(
        "bsc", FAKE_POOL, "router", "manual", "dex_aggregator", "x", review_status=ReviewStatus.CONFIRMED
    )
    store = _Store([manual])
    got = identify(Chain.BSC, [FAKE_POOL], store=store, reader=_reader([]), code_reader=_codes)
    assert got[FAKE_POOL] is manual and store.writes == 0


def test_registry_discovery_lists_venus_markets():
    markets = json.loads((Path(__file__).parent / "fixtures" / "venus_core_markets.json").read_text())["markets"]
    reader = lambda batch: {r.key: encode(["address[]"], [markets]) for r in batch}  # noqa: E731
    records = discover_registries(Chain.BSC, reader)
    assert {r.address for r in records} == set(markets)
    assert {(r.kind, r.source, r.family, r.instance_key) for r in records} == {
        ("market", "registry_call", "compound_v2_like", "venus-core")
    }
