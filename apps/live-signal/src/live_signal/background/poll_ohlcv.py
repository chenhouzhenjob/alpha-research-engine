"""K 线轮询：定时调用 GeckoTerminal 的分钟级 OHLCV 接口，写入 `pool_ohlcv` 表（`tf='1m'`）。

替代的是"自己订阅 Swap 事件 + 现场聚合"的路径（`apps/lp-backtest` 的
`aggregate_candles.py`，仍保留代码但不再是定时数据来源）——GeckoTerminal 自己的索引管线
已经把"逐笔成交聚合成分钟 K 线"这件事做了，实测延迟约 2 分钟，直接轮询比自建聚合简单得多，
也不需要 `swap_events` 表参与。

GeckoTerminal 的 OHLCV 只给一个 volume 数字（`currency=token` 口径下是 base token 成交量），
没有 quote_volume、没有 trade_count——这两个字段这里恒为 None（三态约定：拿不到不能编，
不是"当作 0"）。

`GeckoTerminalClient` 内部是同步的（`requests` + 自带节流 sleep），这里用 `asyncio.to_thread`
包一层，避免堵住 FastAPI 的事件循环（跟 `subscribe.py` 里对同步链上调用的处理是同一个道理）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from alpha_core.instrument_id import MarketType, build_instrument_id
from alpha_core.models import OhlcvCandle
from alpha_datasources.geckoterminal import GeckoTerminalClient
from alpha_storage.db import session_scope
from alpha_storage.repositories.ohlcv import OhlcvRepository

logger = logging.getLogger(__name__)

TF_1M = "1m"
DEFAULT_LIMIT = 10  # 每次轮询取最近 10 根 1 分钟线，覆盖轮询间隔之间可能的漏拍，幂等 upsert 不怕重复
POLL_INTERVAL_SECONDS = 60.0
# 阶段 0 只有一个协议（PancakeSwap V3），venue 直接用这个字面量而不是从 pool_candidates.dex_id
# 读回来比较——CHAR(30) 读回来会带尾随空格 padding，拿去跟这个字面量做原始 Python 比较会
# 静默出错（instrument_id 从 CHAR 改成 TEXT 就是因为踩过这个坑，见实施记录）。
_PANCAKESWAP_V3_BSC_VENUE = "pancakeswap-v3-bsc"


def _poll_pool_sync(gecko: GeckoTerminalClient, pool_address: str, *, limit: int) -> int:
    points = gecko.get_minute_ohlcv(pool_address, limit=limit)
    if not points:
        return 0

    instrument_id = build_instrument_id(
        venue=_PANCAKESWAP_V3_BSC_VENUE, market_type=MarketType.DEX_POOL, symbol_raw=pool_address
    )
    now = datetime.now(UTC)
    candles = [
        OhlcvCandle(
            instrument_id=instrument_id,
            venue=_PANCAKESWAP_V3_BSC_VENUE,
            tf=TF_1M,
            ts_event=p.ts_event,
            ts_ingest=now,
            open=p.open,
            high=p.high,
            low=p.low,
            close=p.close,
            volume=p.volume,
            quote_volume=None,  # GeckoTerminal 只给一个 volume 数字，没有单独的 quote 侧
            trade_count=None,  # GeckoTerminal 的 OHLCV 不带成交笔数
        )
        for p in points
    ]
    with session_scope() as session:
        OhlcvRepository(session).upsert_many(candles)
    return len(candles)


async def run_forever(pool_addresses: list[str], *, interval_seconds: float = POLL_INTERVAL_SECONDS) -> None:
    """常驻轮询入口，`main.py` 的 lifespan 用 `asyncio.create_task` 启动，应用关闭时取消。

    单个池子轮询失败不能拖垮整个循环（比如 GeckoTerminal 该池子暂时没数据、瞬时网络错误）——
    记日志跳过，等下一轮重试，跟其他后台任务"单点失败不放大"的处理原则一致。
    """
    gecko = GeckoTerminalClient(network="bsc")
    logger.info("开始轮询 %d 个池子的 K 线（每 %.0f 秒一次）", len(pool_addresses), interval_seconds)
    while True:
        for pool_address in pool_addresses:
            try:
                count = await asyncio.to_thread(_poll_pool_sync, gecko, pool_address, limit=DEFAULT_LIMIT)
                logger.info("%s: 写入 %d 根 1m K 线", pool_address, count)
            except Exception:  # noqa: BLE001 - 单个池子这一轮失败不能让整个轮询循环退出
                logger.exception("%s 本轮 K 线轮询失败，等下一轮重试", pool_address)
        await asyncio.sleep(interval_seconds)
