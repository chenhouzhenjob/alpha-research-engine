"""链上池子 K 线（`pool_ohlcv` 表）仓储。"""

from __future__ import annotations

from alpha_core.models import OhlcvCandle
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import OhlcvRow

_DATASET = "ohlcv"


class OhlcvRepository:
    """`pool_ohlcv` 的读写仓储。按 `(instrument_id, tf, ts_event)` 幂等 upsert——聚合脚本阶段 0
    采用"每次全量重算"的策略（见 `lp_backtest.aggregate_candles` 文档），同一根 K 线被重复
    计算是预期行为，冲突时用新值整体覆盖旧值，不是增量累加。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, candles: list[OhlcvCandle]) -> None:
        if not candles:
            return
        rows = [
            {
                "dataset": _DATASET,
                "instrument_id": c.instrument_id,
                "venue": c.venue,
                "tf": c.tf,
                "ts_event": c.ts_event,
                "ts_ingest": c.ts_ingest,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "quote_volume": c.quote_volume,
                "trade_count": c.trade_count,
            }
            for c in candles
        ]
        stmt = insert(OhlcvRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_pool_ohlcv_instrument_tf_ts",
            set_={
                "ts_ingest": stmt.excluded.ts_ingest,
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "quote_volume": stmt.excluded.quote_volume,
                "trade_count": stmt.excluded.trade_count,
            },
        )
        self._session.execute(stmt)

    def get_recent(self, instrument_id: str, tf: str, limit: int) -> list[OhlcvRow]:
        """取最近 `limit` 根 K 线，按 `ts_event` 升序返回——跟 `features/` 下其他函数
        （`sigma_price`/`capital_volatility`）"调用方传升序序列"的既有约定一致。数据库层面按
        DESC 取（用得上索引直接拿到最新的 N 条），拿到手之后再 reverse，不是让数据库做升序
        扫描再 LIMIT（那样在大表上找"最新 N 条"效率更差）。
        """
        stmt = (
            select(OhlcvRow)
            .where(OhlcvRow.instrument_id == instrument_id, OhlcvRow.tf == tf)
            .order_by(OhlcvRow.ts_event.desc())
            .limit(limit)
        )
        rows = list(self._session.scalars(stmt))
        rows.reverse()
        return rows
