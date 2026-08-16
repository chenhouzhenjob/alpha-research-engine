"""lp-backtest 的准入门槛与白名单配置。

对应 pool-discovery-metrics-v1.md 第 0 节"前置决策"：
候选池需过硬性准入门槛才进入打分——最低 TVL、最低 24h volume、最低池龄、token 白名单。
"""

from __future__ import annotations

# Token 白名单：至少一侧命中才算候选池（CAKE/WBNB/USDT/USDC/BTCB/ETH，BSC 主网地址，小写）。
# 地址已逐个通过 GeckoTerminal /tokens/multi 校验 symbol 匹配。
WHITELIST_TOKENS_BSC: frozenset[str] = frozenset(
    {
        # "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82",  # CAKE
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
        "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",  # BTCB
        "0x2170ed0880ac9a755fd29b2688956bd959f933f8",  # ETH（Binance-Peg）
    }
)

# 池龄门槛：< 7 天的池子直接不入围（沿用 pool-discovery-metrics-v1.md 1.3 节 AgePenalty 的门槛口径，
# 该节明确写"门槛已经拦掉 < 7 天的池子"）。
MIN_POOL_AGE_DAYS = 7

# TVL / 24h volume 门槛：原文档未给出具体数值（只说"最低 TVL、最低 24h volume"），
# 这里取的是本期经验默认值，不是最终结论——1e 阶段"权重与分档校准"要用真实候选池数据验证这两个阈值是否合理。
MIN_TVL_USD = 10_000.0
MIN_VOLUME_24H_USD = 1_000.0

# 历史快照回填天数：对应设计方案验收标准"近一个月的日线快照"。
DEFAULT_BACKFILL_DAYS = 30


def is_whitelisted_pool(token0_address: str, token1_address: str) -> bool:
    """候选池两侧 token 至少一侧命中白名单才算过 token 门槛。"""
    return token0_address.lower() in WHITELIST_TOKENS_BSC or token1_address.lower() in WHITELIST_TOKENS_BSC
