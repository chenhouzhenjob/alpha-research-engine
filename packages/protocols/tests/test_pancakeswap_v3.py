"""用真实 BSC 链上捕获的日志做回归基准：一条 PoolCreated（block 26957718，PancakeSwap V3
Factory）、一条 Swap（block 116218508，BTC/USDT 池子 0x46cf1c...，WebSocket 订阅实测捕获）。

固定样本，不发起任何网络请求。
"""

from datetime import UTC, datetime

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


# 通过 EvmWebSocketSubscriber 对 BTC/USDT 池子实测订阅捕获的一条真实 Swap 日志。
REAL_SWAP_LOG = LogEntry(
    address="0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4",
    topics=[
        "19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83",
        "000000000000000000000000b5cb0550811d3ded8203b64a51b8c6781644df4e",
        "000000000000000000000000b5cb0550811d3ded8203b64a51b8c6781644df4e",
    ],
    data=(
        "fffffffffffffffffffffffffffffffffffffffffffffffbc3b2398aaf8bc60b"
        "00000000000000000000000000000000000000000000000000046705c6365fec"
        "00000000000000000000000000000000000000000104edd0408c1ad0815bf9ed"
        "0000000000000000000000000000000000000000000088822d343502d256a903"
        "fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffe504"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000000000000000000310c4b252e"
    ),
    block_number=116218508,
    log_index=98,
    transaction_hash="00fefaefd6ab2550c4b23952a30ea93633156bd6914cb430030cc4a3fc695943",
)


def test_swap_topic0_matches_real_bsc_log():
    plugin = PancakeswapV3Plugin()
    assert plugin.swap_topic0() == "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83"


def test_decode_swap_event_matches_real_bsc_log():
    plugin = PancakeswapV3Plugin()
    fetched_at = datetime(2026, 8, 16, tzinfo=UTC)
    block_time = datetime(2026, 8, 16, 5, 53, 30, tzinfo=UTC)
    event = plugin.decode_swap_event(REAL_SWAP_LOG, fetched_at=fetched_at, block_time=block_time)

    assert event.chain == Chain.BSC
    assert event.pool_address == "0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4"
    assert event.instrument_id == "pancakeswap-v3-bsc:dex_pool:0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4"
    assert event.tx_hash == "00fefaefd6ab2550c4b23952a30ea93633156bd6914cb430030cc4a3fc695943"
    assert event.log_index == 98
    assert event.block_number == 116218508
    assert event.sender == "0xb5cb0550811d3ded8203b64a51b8c6781644df4e"
    assert event.recipient == "0xb5cb0550811d3ded8203b64a51b8c6781644df4e"
    assert event.amount0 == -78132323717483870709  # token0（USDT）流出，带符号
    assert event.amount1 == 1239174404792300  # token1（BTCB）流入
    assert event.tick_after == -110528
    assert event.fetched_at == fetched_at
    assert event.block_time == block_time

    # sqrtPriceX96 换算出的 token1/token0 价格应在真实 BTC/USDT 量级（~6 万美元/BTC）
    price = (event.sqrt_price_x96_after / 2**96) ** 2
    assert 50_000 < 1 / price < 80_000
