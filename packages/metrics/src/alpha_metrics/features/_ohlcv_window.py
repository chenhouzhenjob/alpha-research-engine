"""ATR/ADX 共用的 OHLCV 读取 + 重采样。下划线前缀——内部辅助模块，不是公开特征
（跟 `features/` 下其他文件不同，这个不直接对外提供一个"指标"，只是两个指标共用的管道）。

ATR/ADX 用 5 分钟K线，不用落库的原始 1 分钟K线——1 分钟粒度会被链上出块噪音主导，
对做市调仓这种日级别的决策没有意义，`volatility.sigma_price`（日线）已经覆盖了"慢"的一端，
ATR/ADX 覆盖"快"的一端，两者故意不同粒度。没有单独起一张 5 分钟K线表/轮询任务——
按需从已经落库的 1 分钟K线重采样，纯计算，不新增数据源。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from alpha_storage.repositories.ohlcv import OhlcvRepository
from sqlalchemy.orm import Session

RESAMPLE_BUCKET_MINUTES = 5
# ADX 最多需要 2*period 根 5 分钟K线（见 adx.py），period 用 features/adx.py 的 DEFAULT_PERIOD=14
# 时是 28 根 -> 至少 140 根 1 分钟K线；取 200 留缓冲，容忍下面重采样允许的桶内缺口。
FETCH_LIMIT_1M = 200


@dataclass(frozen=True)
class Candle:
    """跟 `alpha_storage.models.OhlcvRow` 字段对齐的精简视图，ATR/ADX 只需要这几个字段，
    不需要 `OhlcvRow` 的 `venue`/`dataset`/`quote_volume`/`trade_count` 等其余字段。
    """

    ts_event: datetime
    open: float
    high: float
    low: float
    close: float


def fetch_recent_1m_candles(session: Session, instrument_id: str, *, limit: int = FETCH_LIMIT_1M) -> list[Candle]:
    """从 `pool_ohlcv` 取最近 `limit` 根 1 分钟K线（升序），映射成精简的 `Candle`。"""
    rows = OhlcvRepository(session).get_recent(instrument_id, tf="1m", limit=limit)
    return [
        Candle(ts_event=r.ts_event, open=float(r.open), high=float(r.high), low=float(r.low), close=float(r.close))
        for r in rows
    ]


def resample_to_5m(candles_1m: list[Candle]) -> list[Candle]:
    """把升序 1 分钟K线按 5 分钟对齐分桶（`:00`/`:05`/`:10`…），合成 5 分钟 OHLC。

    **丢弃末尾还没走完 5 分钟的那个桶**——这是真实的正确性细节，不是可选的严谨：如果不丢，
    最新一根 K 线会用一个还没结束的时间区间冒充完整 K 线，ATR/ADX 每次调用都会悄悄拿一根
    "半成品"K线参与计算，尤其是 high/low 会被人为压窄（区间还没走完，极值还没出现）。

    桶内允许有缺口（同一个 5 分钟桶里 1 分钟K线不满 5 根也照样产出，只要 >=1 根）——
    `poll_ohlcv` 本来就会偶尔漏拍（单个池子这一轮轮询失败会跳过，见其模块文档），
    不能因为一次轮询失败就让整个重采样失败。
    """
    if not candles_1m:
        return []

    buckets: dict[datetime, list[Candle]] = {}
    for c in candles_1m:
        bucket_start = c.ts_event.replace(
            minute=(c.ts_event.minute // RESAMPLE_BUCKET_MINUTES) * RESAMPLE_BUCKET_MINUTES,
            second=0,
            microsecond=0,
        )
        buckets.setdefault(bucket_start, []).append(c)

    now_bucket_start = candles_1m[-1].ts_event.replace(
        minute=(candles_1m[-1].ts_event.minute // RESAMPLE_BUCKET_MINUTES) * RESAMPLE_BUCKET_MINUTES,
        second=0,
        microsecond=0,
    )
    last_complete_bucket = now_bucket_start - timedelta(minutes=RESAMPLE_BUCKET_MINUTES)

    result = []
    for bucket_start in sorted(buckets):
        if bucket_start > last_complete_bucket:
            continue  # 还没走完 5 分钟的桶，丢弃（见上面文档）
        bucket_candles = buckets[bucket_start]
        result.append(
            Candle(
                ts_event=bucket_start,
                open=bucket_candles[0].open,
                high=max(c.high for c in bucket_candles),
                low=min(c.low for c in bucket_candles),
                close=bucket_candles[-1].close,
            )
        )
    return result
