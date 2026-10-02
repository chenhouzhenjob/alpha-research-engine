"""Compound V2 家族（Venus 等借贷市场的分叉）。

- `decoder.py`：存款、取款、借款、还款（含代还）、清算（借款人、清算人两个视角）、领奖励；
- `valuation.py`：存款（按汇率）、负债、待领奖励。

市场（vToken）由 Comptroller.getAllMarkets() 发现（识别第一层的 registry_call，步骤 12），
识别结果的 kind 为 market。
"""

from __future__ import annotations

from types import MappingProxyType

from eth_abi import decode as abi_decode
from eth_utils import keccak

from ...config._common import Address
from ...decoding.models import PositionCategory, PositionKind
from ...identification.plans import DiscoveryPlan, RegistryCall
from ..base import FamilyOptions, ProtocolFamily
from .decoder import BORROW, LIQUIDATE, MINT, REDEEM, REPAY, CompoundV2Decoder
from .valuation import CompoundV2Valuer


class CompoundV2Options(FamilyOptions):
    """Compound V2 系实例的配置项。"""

    native_market: Address | None = None  # 底层资产为原生币的市场（vBNB、cETH）；没有则不填
    reward_token: Address | None = None  # 协议奖励 token（Venus 为 XVS，Compound 为 COMP）；没有则不填


class CompoundV2Family(ProtocolFamily):
    key = "compound_v2_like"
    version = 1
    roles = frozenset({"comptroller"})  # Comptroller（Unitroller 代理地址）；市场由它的 getAllMarkets() 发现
    options_model = CompoundV2Options
    signature_topics = frozenset({*MINT, *REDEEM, BORROW, REPAY, LIQUIDATE})
    position_categories = MappingProxyType(
        {
            PositionKind.SHARE: PositionCategory.LENDING_SUPPLY,
            PositionKind.DEBT: PositionCategory.LENDING_DEBT,
            PositionKind.CLAIMABLE: PositionCategory.CLAIMABLE_REWARD,
        }
    )

    @classmethod
    def decoder(cls, deployment):
        return CompoundV2Decoder(
            family=cls.key,
            instance_key=deployment.instance_key,
            decoder_version=cls.decoder_version(),
            comptrollers=frozenset(deployment.roles["comptroller"]),
            native_market=deployment.options.native_market,
            reward_token=deployment.options.reward_token,
        )

    @classmethod
    def valuer(cls, deployment, profile):
        return CompoundV2Valuer(
            instance_key=deployment.instance_key,
            comptroller=deployment.roles["comptroller"][0],
            native_market=deployment.options.native_market,
            reward_token=deployment.options.reward_token,
        )

    @classmethod
    def discovery(cls, deployment, profile):
        """市场：调用 Comptroller.getAllMarkets()，返回的每个地址登记为 market。"""
        return DiscoveryPlan(
            registry_calls=tuple(
                RegistryCall(
                    instance_key=deployment.instance_key,
                    family=cls.key,
                    to=comptroller,
                    data="0x" + keccak(text="getAllMarkets()").hex()[:8],
                    kind="market",
                    decode=lambda raw: [a.lower() for a in abi_decode(["address[]"], raw)[0]],
                )
                for comptroller in deployment.roles["comptroller"]
            )
        )
