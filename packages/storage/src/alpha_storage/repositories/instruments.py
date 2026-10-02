"""标的目录仓储。"""

from __future__ import annotations

from alpha_core.models import Instrument
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import InstrumentRow


class InstrumentRepository:
    """`instruments` 的读写仓储。按 `instrument_id` 幂等 upsert——人工注册/自动发现都可能
    重复调用同一个标的，冲突时覆盖除 `instrument_id` 外的全部字段（目录信息以最新一次登记为准，
    不像 `pool_candidates` 那样需要保留"首次发现"语义）。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, instrument: Instrument) -> None:
        row = {
            "instrument_id": instrument.instrument_id,
            "venue": instrument.venue,
            "market_type": instrument.market_type,
            "base": instrument.base,
            "quote": instrument.quote,
            "settle": instrument.settle,
            "symbol_raw": instrument.symbol_raw,
            "chain": instrument.chain.value,
            "listed_at": instrument.listed_at,
            "delisted_at": instrument.delisted_at,
            "meta_json": instrument.meta_json,
        }
        stmt = insert(InstrumentRow).values(row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id"],
            set_={
                "venue": stmt.excluded.venue,
                "market_type": stmt.excluded.market_type,
                "base": stmt.excluded.base,
                "quote": stmt.excluded.quote,
                "settle": stmt.excluded.settle,
                "symbol_raw": stmt.excluded.symbol_raw,
                "chain": stmt.excluded.chain,
                "listed_at": stmt.excluded.listed_at,
                "delisted_at": stmt.excluded.delisted_at,
                "meta_json": stmt.excluded.meta_json,
            },
        )
        self._session.execute(stmt)
