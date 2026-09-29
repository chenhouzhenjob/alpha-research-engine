"""Uniswap V2 家族（PancakeSwap V2、Uniswap V2 等恒定乘积 AMM 的分叉）。

- `decoder.py`：钱包经路由添加 / 移除流动性、交换；`calls.py`：路由调用数据解析；
- `valuation.py`：LP 份额估值（含协议费稀释），与链上 Burn 逐 wei 相等。
"""

from __future__ import annotations

from eth_utils import keccak
from pydantic import Field

from ...identification.plans import Create2Rule, DiscoveryPlan
from ..base import FamilyOptions, ProtocolFamily
from .decoder import PAIR_BURN, PAIR_MINT, PAIR_SWAP, PAIR_SYNC, UniswapV2Decoder
from .valuation import UniswapV2Valuer


class UniswapV2Options(FamilyOptions):
    """Uniswap V2 系实例的配置项。"""

    pair_init_code_hash: str = Field(pattern=r"^0x[0-9a-f]{64}$")  # 交易对 init code 的 keccak，CREATE2 校验交易对用
    # 协议费常数：_mintFee 里 L_fee = ts·(rootK−rootKLast)·n / (rootK·d + rootKLast·n)。
    # Uniswap 为 n=1、d=5（LP 手续费的 1/6 归协议）；PancakeSwap 为 n=8、d=17（0.25% 里 0.08% 归协议）
    fee_numerator: int = Field(gt=0)
    fee_denominator: int = Field(gt=0)


class UniswapV2Family(ProtocolFamily):
    key = "uniswap_v2_like"
    version = 1
    roles = frozenset({"factory", "router"})  # factory：工厂；router：Router02
    options_model = UniswapV2Options
    signature_topics = frozenset({PAIR_MINT, PAIR_BURN, PAIR_SWAP, PAIR_SYNC})

    @classmethod
    def decoder(cls, deployment):
        return UniswapV2Decoder(
            family=cls.key,
            instance_key=deployment.instance_key,
            decoder_version=cls.decoder_version(),
            routers=frozenset(deployment.roles.get("router", ())),
        )

    @classmethod
    def valuer(cls, deployment, profile):
        return UniswapV2Valuer(
            instance_key=deployment.instance_key,
            factory=deployment.roles["factory"][0],
            fee_numerator=deployment.options.fee_numerator,
            fee_denominator=deployment.options.fee_denominator,
        )

    @classmethod
    def discovery(cls, deployment, profile):
        """交易对：读 token0()、token1()，盐 = keccak(token0 ‖ token1)（abi.encodePacked），部署者是工厂。"""
        return DiscoveryPlan(
            create2_rules=(
                Create2Rule(
                    instance_key=deployment.instance_key,
                    family=cls.key,
                    kind="pair",
                    selectors=(_sel("token0()"), _sel("token1()")),
                    read_types=("address", "address"),
                    salt=lambda v: keccak(bytes.fromhex(v[0][2:]) + bytes.fromhex(v[1][2:])),
                    deployer=deployment.roles["factory"][0],
                    init_code_hash=deployment.options.pair_init_code_hash,
                    variant=profile.create2_variant,
                ),
            )
        )


def _sel(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()[:8]
