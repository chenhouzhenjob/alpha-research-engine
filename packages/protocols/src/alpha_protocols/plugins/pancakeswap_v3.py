"""PancakeSwap V3（BSC）协议插件。

PancakeSwap V3 是 Uniswap V3 的字节码级 fork，Factory 的 `PoolCreated` 事件签名与参数顺序
和 Uniswap V3 完全一致：
    event PoolCreated(
        address indexed token0,
        address indexed token1,
        uint24 indexed fee,
        int24 tickSpacing,
        address pool
    )
"""

from __future__ import annotations

from datetime import UTC, datetime

from alpha_chains.base import ChainAdapter, LogEntry
from alpha_core.models import PoolCandidate
from alpha_core.types import Chain, DexId, PoolCandidateStatus
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import Web3

from ..base import FactoryDiscoveryPlugin

# BSC 上的 PancakeSwap V3 Factory 地址，取自 alpha-lp packages/config（bscPancakeV3Deployment.factory）。
FACTORY_ADDRESS = "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"

_POOL_CREATED_SIGNATURE = "PoolCreated(address,address,uint24,int24,address)"

# PancakeSwap V3 官方公开宣布的 BSC 主网上线日期是 2023-04-03（多个新闻源报道于 2023-03-06~04-08 之间，
# 见 lp-backtest 实现记录）。这里往前多留两周安全余量，只是"扫描起点不会晚于真实部署"的下界，
# 不是精确部署时间——真实部署区块由后续 get_logs 扫描直接确认。
LAUNCH_DATE_HINT_UTC = datetime(2023, 3, 20, tzinfo=UTC)

# `Factory.getPool(address,address,uint24)` 的函数选择器，keccak256 签名的前 4 字节
# （已用 Web3.keccak 校验过，和 alpha-lp packages/pancake-v3/src/abi.ts 里用的选择器一致）。
_GET_POOL_SELECTOR = bytes.fromhex("1698ee82")

# 费率（单位 1e-6）→ tickSpacing 的标准映射，PancakeSwap V3 与 Uniswap V3 一致（Factory 部署时固定写死）。
# 已用真实扫描到的池子交叉验证过：fee=500→tickSpacing=10，fee=10000→tickSpacing=200。
FEE_TIER_TICK_SPACING: dict[int, int] = {100: 1, 500: 10, 2500: 50, 10000: 200}

# BSC 上的 MasterChef V3 地址，取自 alpha-lp packages/config（bscPancakeV3Deployment.masterChefV3）。
MASTER_CHEF_V3_ADDRESS = "0x556b9306565093c855aea9ae92a594704c2cd59e"

# 以下选择器均已用 Web3.keccak 重新计算校验过，和 alpha-lp packages/pancake-v3/src/abi.ts 一致。
_SLOT0_SELECTOR = bytes.fromhex("3850c7bd")  # PancakeV3Pool.slot0()
_LM_POOL_SELECTOR = bytes.fromhex("540d4918")  # PancakeV3Pool.lmPool()
_LM_LIQUIDITY_SELECTOR = bytes.fromhex("c3487ff8")  # PancakeV3LmPool.lmLiquidity()
_GET_LATEST_PERIOD_INFO_SELECTOR = bytes.fromhex("a15ea89f")  # MasterChefV3.getLatestPeriodInfo(address)
_LIQUIDITY_SELECTOR = bytes.fromhex("1a686502")  # PancakeV3Pool.liquidity()，当前活跃（in-range）流动性

# cakePerSecond 原始返回值的缩放系数：raw / 1e12（MasterChef 内部精度）/ 1e18（CAKE 是 18 位小数）。
_MASTERCHEF_V3_PRECISION = 10**12
_CAKE_DECIMALS_SCALE = 10**18

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _topic_to_address(topic: str) -> str:
    """从 32 字节 topic 里取出低 20 字节，还原为地址（indexed address 参数的编码方式）。"""
    return "0x" + topic[-40:]


class PancakeswapV3Plugin(FactoryDiscoveryPlugin):
    """PancakeSwap V3（BSC）的工厂发现实现。人工只需要录入 `FACTORY_ADDRESS` 这一个种子地址。"""

    chain = Chain.BSC
    dex_id = DexId.PANCAKESWAP_V3_BSC
    factory_address = FACTORY_ADDRESS

    def pool_created_topic0(self) -> str:
        return Web3.keccak(text=_POOL_CREATED_SIGNATURE).to_0x_hex()

    def launch_date_hint_utc(self) -> datetime:
        return LAUNCH_DATE_HINT_UTC

    def decode_pool_created(self, log: LogEntry) -> PoolCandidate:
        # topics: [topic0, token0, token1, fee]；data: (int24 tickSpacing, address pool)
        _topic0, token0_topic, token1_topic, fee_topic = log.topics
        tick_spacing, pool_address = abi_decode(
            ["int24", "address"], bytes.fromhex(log.data.removeprefix("0x"))
        )
        return PoolCandidate(
            chain=self.chain,
            dex_id=self.dex_id,
            pool_address=pool_address,
            token0_address=_topic_to_address(token0_topic.removeprefix("0x")),
            token1_address=_topic_to_address(token1_topic.removeprefix("0x")),
            fee_pips=int(fee_topic, 16),
            tick_spacing=tick_spacing,
            created_at_block=log.block_number,
            status=PoolCandidateStatus.DISCOVERED,
        )

    def find_pool_by_tokens(
        self, adapter: ChainAdapter, token_a: str, token_b: str, fee: int
    ) -> PoolCandidate | None:
        """直接调用 `Factory.getPool` 查询指定 token 对 + 费率的池子地址，不扫描历史事件。

        只能回答"这个具体 token 对现在有没有池子"，不能像 `decode_pool_created` 那样发现
        "白名单 token 配未知长尾币"的池子——那种场景仍然需要扫描 `PoolCreated` 事件
        （见 `identification/factory_discovery.py`）。

        `created_at_block` 这里用不了（`getPool` 只返回当前地址，不返回部署区块），
        固定为 0 作为"未知"哨兵值；调用方（`qualify.py`）需要退化用 GeckoTerminal
        报告的创建时间兜底池龄判断，不能像扫描发现的候选池一样直接查链上区块时间戳。

        @returns 找到则返回候选池（`created_at_block=0`，`created_at=None`）；不存在返回 None
        """
        if fee not in FEE_TIER_TICK_SPACING:
            raise ValueError(f"未知费率档位: {fee}")
        data = _GET_POOL_SELECTOR + abi_encode(
            ["address", "address", "uint24"],
            [Web3.to_checksum_address(token_a), Web3.to_checksum_address(token_b), fee],
        )
        result = adapter.call(to=self.factory_address, data="0x" + data.hex())
        pool_address_int = int.from_bytes(result[-20:], "big")
        if pool_address_int == 0:
            return None
        token0, token1 = sorted([token_a.lower(), token_b.lower()])
        return PoolCandidate(
            chain=self.chain,
            dex_id=self.dex_id,
            pool_address="0x" + result[-20:].hex(),
            token0_address=token0,
            token1_address=token1,
            fee_pips=fee,
            tick_spacing=FEE_TIER_TICK_SPACING[fee],
            created_at_block=0,
            created_at=None,
            status=PoolCandidateStatus.DISCOVERED,
        )

    def read_fee_protocol(self, adapter: ChainAdapter, pool_address: str) -> tuple[int, int]:
        """读取池子 `slot0()` 里打包的 `feeProtocol0`/`feeProtocol1`（单位 1/10000）。

        `slot0()` 按顺序返回 7 个字：sqrtPriceX96/tick/observationIndex/
        observationCardinality/observationCardinalityNext/feeProtocol/unlocked，
        第 6 个（index 5）就是打包后的 feeProtocol：低 16 位是 feeProtocol0，高 16 位是 feeProtocol1
        （解包方式与 alpha-lp `packages/pancake-v3/src/readonly.ts` 的 `slot0` 解码一致）。

        @returns (feeProtocol0, feeProtocol1)，用于 `features/fee_apr.py` 算 `lpNetShare`
        """
        result = adapter.call(to=pool_address, data="0x" + _SLOT0_SELECTOR.hex())
        word5 = result[5 * 32 : 6 * 32]
        packed = int.from_bytes(word5, "big")
        return packed & 0xFFFF, (packed >> 16) & 0xFFFF

    def read_cake_emission(self, adapter: ChainAdapter, pool_address: str) -> tuple[float, float] | None:
        """读取该池当前的 CAKE 挖矿排放速率与"参与挖矿的流动性占比"（均为链上当前快照值）。

        排放速率数据源对应 alpha-lp `estimateCakeDaily` 里"新开仓"路径：
        `PancakeV3Pool.lmPool()` → `PancakeV3LmPool.lmLiquidity()`（boosted 流动性）；
        `MasterChefV3.getLatestPeriodInfo(pool)` → cakePerSecond + 排放截止时间。

        `lmLiquidityShare` 在这里定义为 `lmLiquidity / 池子当前活跃流动性`（`PancakeV3Pool.liquidity()`），
        不是 alpha-lp 单个仓位场景下的"用户仓位 / (lmLiquidity+用户仓位)"——池子发现场景没有具体仓位，
        这个比例回答的是"这个池子当前活跃流动性里，有多大比例已经质押进 MasterChef 吃 CAKE"，
        没有质押的那部分活跃流动性拿不到 CAKE，如果直接按 `lmLiquidityShare=1` 算会系统性高估 CakeAPR。

        @returns `(cakePerSecond, lmLiquidityShare)`；该池没有挂 CAKE farm、排放已结束/为零、
        或当前活跃流动性为 0 时返回 `None`（调用方应当把这种情况当作 `MetricAvailability.NO_INCENTIVE`，
        不是 `UNAVAILABLE`——这是"确认当前无激励"，不是"数据取不到"）。
        """
        lm_pool_raw = adapter.call(to=pool_address, data="0x" + _LM_POOL_SELECTOR.hex())
        lm_pool_address = "0x" + lm_pool_raw[-20:].hex()
        if lm_pool_address == _ZERO_ADDRESS:
            return None

        lm_liquidity_raw = adapter.call(to=lm_pool_address, data="0x" + _LM_LIQUIDITY_SELECTOR.hex())
        lm_liquidity = int.from_bytes(lm_liquidity_raw, "big")

        active_liquidity_raw = adapter.call(to=pool_address, data="0x" + _LIQUIDITY_SELECTOR.hex())
        active_liquidity = int.from_bytes(active_liquidity_raw, "big")
        if active_liquidity == 0:
            return None

        period_data = _GET_LATEST_PERIOD_INFO_SELECTOR + abi_encode(
            ["address"], [Web3.to_checksum_address(pool_address)]
        )
        period_raw = adapter.call(to=MASTER_CHEF_V3_ADDRESS, data="0x" + period_data.hex())
        cake_per_second_raw = int.from_bytes(period_raw[0:32], "big")
        end_time = int.from_bytes(period_raw[32:64], "big")

        if cake_per_second_raw == 0 or end_time <= datetime.now(UTC).timestamp():
            return None

        cake_per_second = cake_per_second_raw / _MASTERCHEF_V3_PRECISION / _CAKE_DECIMALS_SCALE
        lm_liquidity_share = min(lm_liquidity / active_liquidity, 1.0)
        return cake_per_second, lm_liquidity_share
