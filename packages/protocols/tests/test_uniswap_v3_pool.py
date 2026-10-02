"""V3 池子通用机制：两种 Swap 变体按 topic0 自动识别、两种 feeProtocol 打包方式。"""

from __future__ import annotations

import pytest
from _golden import load
from alpha_protocols.families.uniswap_v3_like import pool as v3
from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin


def _swap_logs():
    """清算样本里同时有 Uniswap V3（BSC 上的部署）和 PancakeSwap V3 的 Swap 日志。"""
    logs = load("bsc/compound_v2_like/liquidation_borrower").receipt.logs
    return {v3.swap_variant(lg.topics[0]): lg for lg in logs if v3.swap_variant(lg.topics[0])}


def test_both_swap_variants_are_recognized_by_topic0():
    by_variant = _swap_logs()
    assert set(by_variant) == {v3.Variant.UNISWAP, v3.Variant.PANCAKE}
    uni = v3.decode_swap(by_variant[v3.Variant.UNISWAP].topics, by_variant[v3.Variant.UNISWAP].data)
    pcs = v3.decode_swap(by_variant[v3.Variant.PANCAKE].topics, by_variant[v3.Variant.PANCAKE].data)
    assert uni.protocol_fees is None and pcs.protocol_fees is not None
    for swap in (uni, pcs):
        assert (swap.amount0 > 0) != (swap.amount1 > 0), "一边进一边出"
        assert swap.sqrt_price_x96 > 0 and swap.liquidity > 0
    # 数据长度与变体一致：原版 5 个字，Pancake 7 个字
    assert len(by_variant[v3.Variant.UNISWAP].data) == 2 + 64 * 5
    assert len(by_variant[v3.Variant.PANCAKE].data) == 2 + 64 * 7


def test_plugin_swap_topic_is_pancake_variant_and_decodes_legacy_log_format():
    """旧插件用的 LogEntry 的 topic 不带 0x，迁移后仍能解。"""
    from alpha_chains.base import LogEntry

    lg = _swap_logs()[v3.Variant.PANCAKE]
    plugin = PancakeswapV3Plugin()
    assert v3.swap_variant(plugin.swap_topic0()) is v3.Variant.PANCAKE
    legacy = LogEntry(lg.address, [t[2:] for t in lg.topics], lg.data, lg.block_number, lg.log_index, lg.tx_hash[2:])
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    event = plugin.decode_swap_event(legacy, fetched_at=now, block_time=now)
    assert event.amount0 == v3.decode_swap(lg.topics, lg.data).amount0
    assert event.sender.startswith("0x")


def _slot0_bytes(packed_fee: int) -> bytes:
    words = [2**96, (2**256 - 5), 0, 0, 0, packed_fee, 1]  # tick = -5
    return b"".join(w.to_bytes(32, "big") for w in words)


def test_fee_protocol_packing_differs_by_variant():
    pancake = v3.decode_slot0(_slot0_bytes((3300 << 16) | 3200), v3.Variant.PANCAKE)
    assert (pancake.fee_protocol0, pancake.fee_protocol1, pancake.tick) == (3200, 3300, -5)
    uniswap = v3.decode_slot0(_slot0_bytes((6 << 4) | 4), v3.Variant.UNISWAP)
    assert (uniswap.fee_protocol0, uniswap.fee_protocol1) == (4, 6)


def test_get_pool_rejects_unknown_fee_tier():
    with pytest.raises(ValueError, match="未知费率档位"):
        v3.get_pool(None, "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20, 2500, {500: 10, 3000: 60})
