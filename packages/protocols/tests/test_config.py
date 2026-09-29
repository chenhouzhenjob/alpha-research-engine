"""链画像与实例配置的加载和校验。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from alpha_core.types import Chain
from alpha_protocols.config._common import ConfigError
from alpha_protocols.config.chain_profiles import chain_profiles, parse_chain_profile
from alpha_protocols.config.instances import load_instances
from alpha_protocols.decoding.evm.rules import GasModel, SystemTxRule
from alpha_protocols.decoding.models import NATIVE
from alpha_protocols.families.base import FamilyOptions, ProtocolFamily

# ----------------------------------------------------------------------
# 链画像
# ----------------------------------------------------------------------


def test_every_chain_has_a_profile():
    """alpha_core 登记的每条链都必须有链画像，否则那条链无法解码。"""
    assert set(chain_profiles()) == set(Chain)


def test_profiles_declare_chain_differences():
    p = chain_profiles()
    assert p[Chain.BSC].rules.gas_model is GasModel.STANDARD and not p[Chain.BSC].rules.system_tx_rules
    assert p[Chain.BASE].rules.gas_model is GasModel.OP_STACK
    assert p[Chain.BASE].rules.system_tx_rules == {SystemTxRule.OP_DEPOSIT}
    assert p[Chain.ETHEREUM].provider_slugs["ankr"] == "eth"


def test_base_assets_and_asset_ids():
    bsc, eth = chain_profiles()[Chain.BSC], chain_profiles()[Chain.ETHEREUM]
    assert bsc.base_assets[NATIVE] == "BNB"
    assert bsc.base_assets[bsc.wrapped_native] == "WBNB"
    # 包装原生币和原生币是同一种经济资产；各链 USDT 地址不同、asset_id 相同
    assert bsc.asset_id_of(bsc.wrapped_native) == bsc.asset_id_of(NATIVE) == "bnb"
    assert bsc.asset_id_of("0x55d398326f99059ff775485246999027b3197955") == "usdt"
    assert eth.asset_id_of("0xdac17f958d2ee523a2206206994597c13d831ec7") == "usdt"
    assert bsc.asset_id_of("0x" + "12" * 20) is None


BSC_FILE = yaml.safe_load((Path(__file__).parents[1] / "src/alpha_protocols/chains/bsc.yaml").read_text())


def _mutated(fn):
    data = copy.deepcopy(BSC_FILE)
    fn(data)
    return data


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["assets"].pop(0), "恰好有一个"),
        (lambda d: d.update(wrapped_native="0x" + "12" * 20), "wrapped_native 必须也列在 assets"),
        (lambda d: d["assets"][1].update(asset_id="wbnb"), "asset_id 必须相同"),
        (lambda d: d["assets"][2].update(address=d["assets"][2]["address"].upper().replace("0X", "0x")), "小写"),
        (lambda d: d["assets"].append(dict(d["assets"][2])), "重复地址"),
        (lambda d: d.update(gas_model="arbitrum"), "gas_model"),
        (lambda d: d.update(unknown_field=1), "unknown_field"),
        (lambda d: d["assets"][2].pop("decimals"), "symbol 和 decimals"),
    ],
)
def test_invalid_profiles_are_rejected(mutate, message):
    with pytest.raises(ConfigError, match=message):
        parse_chain_profile(_mutated(mutate), source="bsc.yaml")


# ----------------------------------------------------------------------
# 实例配置（用测试专用的假家族）
# ----------------------------------------------------------------------


class _Options(FamilyOptions):
    fee_numerator: int
    fee_denominator: int
    init_code_hash: str | None = None


class _FakeFamily(ProtocolFamily):
    key = "fake_like"
    version = 1
    roles = frozenset({"factory", "router", "wrapped"})
    options_model = _Options


FAMILIES = {"fake_like": _FakeFamily}
A, B, C = ("0x" + c * 40 for c in "abc")


def _write(tmp_path: Path, name: str, data: dict) -> None:
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(data))


def _instance(key="fake-v2", **deployments):
    return {
        "instance_key": key,
        "family": "fake_like",
        "protocol": "Fake",
        "options": {"fee_numerator": 1, "fee_denominator": 5},
        "deployments": deployments or {"ethereum": {"roles": {"factory": [A]}}},
    }


def test_instance_loads_with_shared_and_overridden_options_and_chain_refs(tmp_path):
    _write(
        tmp_path,
        "fake-v2",
        _instance(
            ethereum={"roles": {"factory": [A], "wrapped": ["$chain.wrapped_native"]}},
            bsc={
                "roles": {"factory": [B]},
                "options": {"fee_numerator": 8, "fee_denominator": 17},
                "from_block": 6809737,
            },
        ),
    )
    reg = load_instances(tmp_path, families=FAMILIES)
    inst = reg.instances["fake-v2"]
    eth, bsc = inst.deployments[Chain.ETHEREUM], inst.deployments[Chain.BSC]
    assert (eth.options.fee_numerator, bsc.options.fee_numerator, bsc.options.fee_denominator) == (1, 8, 17)
    assert eth.roles["wrapped"] == (chain_profiles()[Chain.ETHEREUM].wrapped_native,)
    assert bsc.from_block == 6809737
    assert reg.by_address[(Chain.BSC, B)].role == "factory"
    assert [d.instance_key for d in reg.deployments_on(Chain.BASE)] == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({**_instance(), "family": "nope_like"}, "没有注册"),
        (_instance(ethereum={"roles": {"pool": [A]}}), "不属于家族"),
        ({**_instance(), "options": {"fee_numerator": 1}}, "配置项不符合"),
        ({**_instance(), "options": {"fee_numerator": 1, "fee_denominator": 5, "typo": 1}}, "配置项不符合"),
        (_instance(ethereum={"roles": {"factory": ["0xABC"]}}), "小写"),
        (_instance(ethereum={"roles": {"factory": ["$chain.nope"]}}), "小写"),
        (_instance(solana={"roles": {"factory": [A]}}), "deployments"),
        ({**_instance(), "instance_key": "Fake_V2"}, "instance_key"),
    ],
)
def test_invalid_instances_are_rejected(tmp_path, data, message):
    _write(tmp_path, data["instance_key"], data)
    with pytest.raises(ConfigError, match=message):
        load_instances(tmp_path, families=FAMILIES)


def test_filename_must_match_instance_key(tmp_path):
    _write(tmp_path, "other-name", _instance())
    with pytest.raises(ConfigError, match="文件名必须等于"):
        load_instances(tmp_path, families=FAMILIES)


def test_same_address_in_two_instances_on_one_chain_is_rejected(tmp_path):
    _write(tmp_path, "fake-v2", _instance())
    _write(tmp_path, "fake-v3", _instance("fake-v3", ethereum={"roles": {"router": [A]}}))
    with pytest.raises(ConfigError, match="同时是 fake-v2.factory 和 fake-v3.router"):
        load_instances(tmp_path, families=FAMILIES)
    # 不同链上的同一个地址互不影响（同一合约用 CREATE2 部署到多条链很常见）
    _write(tmp_path, "fake-v3", _instance("fake-v3", base={"roles": {"router": [A]}}))
    assert len(load_instances(tmp_path, families=FAMILIES).instances) == 2


def test_packaged_instances_load():
    """包内的实例配置随各家族的步骤加入；无论有多少份，都必须能通过校验。"""
    load_instances()
