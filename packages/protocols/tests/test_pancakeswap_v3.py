"""用真实 BSC 链上捕获的一条 PoolCreated 日志（block 26957718，PancakeSwap V3 Factory）做回归基准。

固定样本，不发起任何网络请求。
"""

from alpha_chains.base import LogEntry
from alpha_core.types import Chain, DexId, PoolCandidateStatus
from alpha_protocols.plugins.pancakeswap_v3 import FACTORY_ADDRESS, PancakeswapV3Plugin

REAL_POOL_CREATED_LOG = LogEntry(
    address=FACTORY_ADDRESS,
    topics=[
        "783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118",
        "00000000000000000000000057a63c32cc2ad6ce4fbe5423d548d12d0eeddfc1",
        "000000000000000000000000db19f2052d2b1ad46ed98c66336a5daadeb13005",
        "00000000000000000000000000000000000000000000000000000000000001f4",
    ],
    data=(
        "000000000000000000000000000000000000000000000000000000000000000a"
        "000000000000000000000000a7619d726f619062d2d2bcadbb2ee1fb1952d6d7"
    ),
    block_number=26957718,
    log_index=72,
    transaction_hash="42f81166f79cb1b72c120ce8379bd74226bf52535c9963cfee056c1797701094",
)


def test_pool_created_topic0_matches_uniswap_v3_signature():
    plugin = PancakeswapV3Plugin()
    assert plugin.pool_created_topic0() == "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"


def test_decode_pool_created_matches_real_bsc_log():
    plugin = PancakeswapV3Plugin()
    candidate = plugin.decode_pool_created(REAL_POOL_CREATED_LOG)

    assert candidate.chain == Chain.BSC
    assert candidate.dex_id == DexId.PANCAKESWAP_V3_BSC
    assert candidate.pool_address == "0xa7619d726f619062d2d2bcadbb2ee1fb1952d6d7"
    assert candidate.token0_address == "0x57a63c32cc2ad6ce4fbe5423d548d12d0eeddfc1"
    assert candidate.token1_address == "0xdb19f2052d2b1ad46ed98c66336a5daadeb13005"
    assert candidate.fee_pips == 500
    assert candidate.tick_spacing == 10  # 0.05% 费率在 Uniswap V3 系里固定对应 tickSpacing=10
    assert candidate.created_at_block == 26957718
    assert candidate.status == PoolCandidateStatus.DISCOVERED
