"""Uniswap V3 家族（PancakeSwap V3、Uniswap V3 等集中流动性 AMM 的分叉）。

- `pool.py`：池子层面的机制（PoolCreated、两种 Swap 变体、getPool、slot0），完全参数化，
  lp-backtest / live-signal 通过 `plugins/pancakeswap_v3.py` 使用；
- `decoder.py`：钱包视角的 NPM 仓位解码；`calls.py`：NPM 调用数据解析；
- 估值随 M2 步骤 8 加入。
"""

from __future__ import annotations

from pydantic import Field

from ..base import FamilyOptions, ProtocolFamily
from .decoder import DECREASE, INCREASE, NPM_COLLECT, UniswapV3Decoder
from .pool import POOL_CREATED, Variant


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

    @classmethod
    def decoder(cls, deployment):
        return UniswapV3Decoder(
            family=cls.key,
            instance_key=deployment.instance_key,
            decoder_version=cls.decoder_version(),
            position_managers=frozenset(deployment.roles.get("position_manager", ())),
        )
