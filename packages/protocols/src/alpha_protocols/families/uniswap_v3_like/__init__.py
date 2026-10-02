"""Uniswap V3 家族（PancakeSwap V3、Uniswap V3 等集中流动性 AMM 的分叉）。

- `pool.py`：池子层面的机制（PoolCreated、两种 Swap 变体、getPool、slot0），完全参数化，
  lp-backtest / live-signal 通过 `plugins/pancakeswap_v3.py` 使用；
- `decoder.py`：钱包视角的 NPM 仓位解码；`calls.py`：NPM 调用数据解析；
- `valuation.py`、`math.py`：NFT 仓位估值（整数 TickMath，与链上逐 wei 相等）。
"""

from __future__ import annotations

from types import MappingProxyType

from eth_abi import encode as abi_encode
from eth_utils import keccak
from pydantic import Field

from ...decoding.models import PositionCategory, PositionKind
from ...identification.plans import Create2Rule, DiscoveryPlan
from ..base import FamilyOptions, ProtocolFamily
from .decoder import DECREASE, INCREASE, NPM_COLLECT, UniswapV3Decoder
from .pool import POOL_CREATED, Variant
from .valuation import UniswapV3Valuer


class UniswapV3Options(FamilyOptions):
    """Uniswap V3 系实例的配置项。"""

    variant: Variant  # 分叉变体：决定 slot0 里 feeProtocol 的打包方式（事件变体按 topic0 自动识别）
    pool_init_code_hash: str = Field(pattern=r"^0x[0-9a-f]{64}$")  # 池子合约 init code 的 keccak，CREATE2 校验池子用
    fee_tick_spacing: dict[int, int]  # 费率（1e-6）→ tickSpacing；各分叉档位不同（Uniswap 有 3000，Pancake 有 2500）


class UniswapV3Family(ProtocolFamily):
    key = "uniswap_v3_like"
    version = 1
    # factory：工厂；pool_deployer：部署池子的合约（PancakeSwap 与工厂分开，Uniswap 就是工厂，不填）；
    # position_manager：NonfungiblePositionManager
    roles = frozenset({"factory", "pool_deployer", "position_manager"})
    options_model = UniswapV3Options
    signature_topics = frozenset({POOL_CREATED, INCREASE, DECREASE, NPM_COLLECT})
    position_categories = MappingProxyType({PositionKind.NFT: PositionCategory.CONCENTRATED_LP})

    @classmethod
    def decoder(cls, deployment):
        return UniswapV3Decoder(
            family=cls.key,
            instance_key=deployment.instance_key,
            decoder_version=cls.decoder_version(),
            position_managers=frozenset(deployment.roles.get("position_manager", ())),
        )

    @classmethod
    def valuer(cls, deployment, profile):
        roles = deployment.roles
        deployer = (roles.get("pool_deployer") or roles["factory"])[0]
        return UniswapV3Valuer(
            instance_key=deployment.instance_key,
            position_manager=roles["position_manager"][0],
            pool_deployer=deployer,
            init_code_hash=deployment.options.pool_init_code_hash,
            create2_variant=profile.create2_variant,
        )

    @classmethod
    def discovery(cls, deployment, profile):
        """池子：读 token0()、token1()、fee()，盐 = keccak(abi.encode(token0, token1, fee))，部署者见 valuer。"""
        roles = deployment.roles
        return DiscoveryPlan(
            create2_rules=(
                Create2Rule(
                    instance_key=deployment.instance_key,
                    family=cls.key,
                    kind="pool",
                    selectors=(_sel("token0()"), _sel("token1()"), _sel("fee()")),
                    read_types=("address", "address", "uint24"),
                    salt=lambda v: keccak(abi_encode(["address", "address", "uint24"], list(v))),
                    deployer=(roles.get("pool_deployer") or roles["factory"])[0],
                    init_code_hash=deployment.options.pool_init_code_hash,
                    variant=profile.create2_variant,
                ),
            )
        )


def _sel(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()[:8]
