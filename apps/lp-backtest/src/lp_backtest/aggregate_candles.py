"""K 线聚合 CLI：读取 `swap_events`，按 1 分钟分桶聚合成 OHLCV，写入 `pool_ohlcv` 表（`tf='1m'`）。

**不再是 `pool_ohlcv` 表的定时数据来源**——`pool_ohlcv` 现在由 `apps/live-signal` 常驻进程里的
`background/poll_ohlcv.py` 每分钟轮询（直接调 GeckoTerminal 的分钟级 OHLCV 接口，比自己
聚合简单），这个文件保留是因为逻辑仍然正确，可以在需要交叉校验 GeckoTerminal 数字、或者
GeckoTerminal 对某个冷门池子没有数据时手动跑。`swap_events`/WebSocket 订阅
（`apps/live-signal` 的 `background/subscribe.py`，默认关闭）本身继续保留——不是为了喂给
这个聚合脚本，是逐笔明细以后风控信号（大户集中度等）要用。

和订阅进程是独立的两步——订阅只管把逐笔事件落库，聚合是手动跑的下一步，
职责分开，跟设计文档 3.5 节一致。

价格换算：token1/token0 汇率 = `(sqrtPriceX96/2^96)^2 * 10^(decimals0 - decimals1)`——
`sqrtPriceX96` 编码的是"两个 token 最小单位"的比例，两个 token 精度不同时不做这个换算会得到
不符合真实价格的数字（1c 阶段吃过 USD/token 汇率混用的教训，这次直接按对的口径设计）。
`decimals` 用 `alpha_metrics.chain_reads.read_decimals_cached` 查询并永久缓存到 `tokens` 表，
不假设固定 18（虽然本期两个试跑池子的三个 token 实测都是 18，但这个系统设计成"适用所有
交易对"，不能把这次巧合当成通用假设）——之前是每次调用都现场 `eth_call` 重查，`decimals()`
是 ERC20 标准里的不可变值，这是纯浪费的链上调用，真实跑过才发现，见 `TokenRow` 的注释。

分桶用真实区块时间，直接读 `swap_events.block_time`（`subscribe_swaps` 落库时已经存好，
见其模块文档），不再现场调 `ChainAdapter.get_block_timestamp`，也不是 `swap_events.fetched_at`
（那是"进程收到/回填时的时间"，跟"这笔交易实际发生的时间"是两回事）。

**这是修过一次真实性能问题之后的版本**：最早的实现每次调用都要对每个不同区块现场查一次
`get_block_timestamp`，而 `EvmChainAdapter` 的缓存是进程内存级别的，这个 CLI 每次调用都是
新进程——实测对 ~1300 个不同区块跑一遍要 6 分钟左右，且随 `swap_events` 持续增长只会越来越慢，
完全撑不住设计文档"定时（如每分钟）聚合"的预期。修法是让 `subscribe_swaps` 落库时就把区块
时间存进 `swap_events.block_time`（WebSocket 订阅 payload 里免费带了这个字段），这里直接读，
不用重查。加这一列之前落库的历史行 `block_time` 是 NULL，遇到时现查一次并直接写回那一行
（见 `_aggregate_pool`），不是每次都重新兜底查——历史行只会经历一次这个较慢的路径，
写回之后下次就直接读到值了，不会随时间反复付出这个成本。

阶段 0 策略：每次调用都对目标池子的全部 `swap_events`重新聚合（不是增量只算新区块），
幂等 upsert 到 `pool_ohlcv`——数据量小（几个池子、几千行事件），重算成本可以忽略，换来的是不用
处理"上次算到哪、新数据会不会影响已经写过的那根 K 线边界"这类增量聚合的边界情况。
数据量大了以后再优化成增量，不是这次的范围。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime

import click
from alpha_chains.bsc import build_bsc_adapter
from alpha_core.instrument_id import MarketType, build_instrument_id
from alpha_core.models import OhlcvCandle
from alpha_core.types import Chain, PoolCandidateStatus
from alpha_metrics.chain_reads import read_decimals_cached
from alpha_storage.db import session_scope
from alpha_storage.models import SwapEventRow
from alpha_storage.repositories.ohlcv import OhlcvRepository
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository
from alpha_storage.repositories.swap_events import SwapEventRepository
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

TF_1M = "1m"
# 阶段 0 只有一个协议（PancakeSwap V3），venue 直接用这个字面量而不是从 pool_candidates.dex_id
# 读回来比较——CHAR(30) 读回来会带尾随空格 padding，拿去跟这个字面量做原始 Python 比较会
# 静默出错（instrument_id 从 CHAR 改成 TEXT 就是因为踩过这个坑，见实施记录）。
_PANCAKESWAP_V3_BSC_VENUE = "pancakeswap-v3-bsc"


def _price_from_sqrt(sqrt_price_x96: int, decimals0: int, decimals1: int) -> float:
    raw_ratio = (sqrt_price_x96 / 2**96) ** 2
    return raw_ratio * 10 ** (decimals0 - decimals1)


def _floor_to_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _aggregate_pool(session, adapter, chain: Chain, pool_address: str) -> int:
    events: list[SwapEventRow] = SwapEventRepository(session).get_since_block(
        chain, pool_address, from_block=0
    )
    if not events:
        return 0

    candidate = PoolCandidateRepository(session).get_by_address(chain, pool_address)
    if candidate is None:
        logger.warning("池子 %s 不在 pool_candidates 里，跳过（缺 token0/token1 精度信息）", pool_address)
        return 0
    decimals0 = read_decimals_cached(session, adapter, chain, candidate.token0_address)
    decimals1 = read_decimals_cached(session, adapter, chain, candidate.token1_address)

    legacy_rows = [e for e in events if e.block_time is None]
    if legacy_rows:
        # 加 block_time 列之前落库的历史行——现场查一次并直接写回这一行，下次跑就不会再是
        # NULL 了（不是每次都重新兜底查：那样等于没修，只是把 6 分钟分摊到"历史行第一次
        # 被聚合时"这一次性成本上，之后新写入的行本来就有 block_time，不会再退化）。
        logger.warning(
            "%s: %d 条历史事件没有 block_time（加列之前落库的），现场查一次并写回", pool_address, len(legacy_rows)
        )
        legacy_blocks = sorted({e.block_number for e in legacy_rows})
        legacy_block_times = {b: adapter.get_block_timestamp(b) for b in legacy_blocks}
        for e in legacy_rows:
            e.block_time = legacy_block_times[e.block_number]
        session.flush()

    buckets: dict[datetime, list[SwapEventRow]] = defaultdict(list)
    for e in events:
        bucket_start = _floor_to_minute(e.block_time)
        buckets[bucket_start].append(e)

    instrument_id = build_instrument_id(
        venue=_PANCAKESWAP_V3_BSC_VENUE, market_type=MarketType.DEX_POOL, symbol_raw=pool_address
    )
    now = datetime.now(UTC)
    candles: list[OhlcvCandle] = []
    for bucket_start, bucket_events in buckets.items():
        bucket_events.sort(key=lambda e: (e.block_number, e.log_index))
        prices = [_price_from_sqrt(e.sqrt_price_x96_after, decimals0, decimals1) for e in bucket_events]
        volume = sum(abs(e.amount0) for e in bucket_events) / 10**decimals0
        quote_volume = sum(abs(e.amount1) for e in bucket_events) / 10**decimals1
        candles.append(
            OhlcvCandle(
                instrument_id=instrument_id,
                venue=_PANCAKESWAP_V3_BSC_VENUE,
                tf=TF_1M,
                ts_event=bucket_start,
                ts_ingest=now,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=volume,
                quote_volume=quote_volume,
                trade_count=len(bucket_events),
            )
        )

    OhlcvRepository(session).upsert_many(candles)
    return len(candles)


@click.command()
@click.option(
    "--pool-address",
    "pool_addresses",
    multiple=True,
    help="只聚合指定池子（可重复传入）；不传则聚合全部 status=qualified 的候选池",
)
def main(pool_addresses: tuple[str, ...]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    chain = Chain.BSC
    adapter = build_bsc_adapter()

    if pool_addresses:
        addresses = [addr.lower() for addr in pool_addresses]
    else:
        with session_scope() as session:
            rows = PoolCandidateRepository(session).list_by_status(chain, PoolCandidateStatus.QUALIFIED)
            addresses = [row.pool_address for row in rows]

    if not addresses:
        logger.warning("没有任何 status=qualified 的候选池，无事可聚合")
        return

    total_candles = 0
    for pool_address in addresses:
        with session_scope() as session:
            count = _aggregate_pool(session, adapter, chain, pool_address)
        logger.info("%s: 聚合出 %d 根 1m K 线", pool_address, count)
        total_candles += count

    logger.info("聚合完成，共 %d 根 K 线（%d 个池子）", total_candles, len(addresses))


if __name__ == "__main__":
    main()
