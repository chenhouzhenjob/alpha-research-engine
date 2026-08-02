"""SQLite catalog：instruments 与采集水位。"""

from __future__ import annotations

import aiosqlite

from alpha.schema import Instrument

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id TEXT PRIMARY KEY,
    venue TEXT NOT NULL,
    market_type TEXT NOT NULL,
    base TEXT NOT NULL,
    quote TEXT NOT NULL,
    settle TEXT,
    symbol_raw TEXT NOT NULL,
    listed_at INTEGER,
    delisted_at INTEGER,
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS watermarks (
    venue TEXT NOT NULL,
    dataset TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    tf TEXT NOT NULL DEFAULT '',
    ts_ms INTEGER NOT NULL,
    PRIMARY KEY (venue, dataset, instrument_id, tf)
);
"""


class Catalog:
    """异步 SQLite 元数据存储。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """打开连接并确保表存在。"""
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA_SQL)
        await self._migrate_legacy_binance_source_watermarks()
        await self._db.commit()

    async def _migrate_legacy_binance_source_watermarks(self) -> None:
        """将旧 profile 名水位合并为 canonical venue，保留较新的时间点。"""
        await self.db.execute(
            """
            INSERT INTO watermarks (venue, dataset, instrument_id, tf, ts_ms)
            SELECT 'binance', dataset, instrument_id, tf, ts_ms
            FROM watermarks
            WHERE venue IN ('binance_perp', 'binance_spot')
            ON CONFLICT(venue, dataset, instrument_id, tf) DO UPDATE SET
                ts_ms = MAX(watermarks.ts_ms, excluded.ts_ms)
            """
        )
        await self.db.execute(
            "DELETE FROM watermarks WHERE venue IN ('binance_perp', 'binance_spot')"
        )

    async def close(self) -> None:
        """关闭连接。"""
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Catalog 未打开，请先调用 open()")
        return self._db

    async def upsert_instruments(self, instruments: list[Instrument]) -> int:
        """插入或更新标的，返回写入条数。"""
        sql = """
        INSERT INTO instruments (
            instrument_id, venue, market_type, base, quote, settle,
            symbol_raw, listed_at, delisted_at, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(instrument_id) DO UPDATE SET
            venue=excluded.venue,
            market_type=excluded.market_type,
            base=excluded.base,
            quote=excluded.quote,
            settle=excluded.settle,
            symbol_raw=excluded.symbol_raw,
            listed_at=excluded.listed_at,
            delisted_at=excluded.delisted_at,
            meta_json=excluded.meta_json
        """
        rows = [
            (
                i.instrument_id,
                i.venue,
                i.market_type,
                i.base,
                i.quote,
                i.settle,
                i.symbol_raw,
                i.listed_at,
                i.delisted_at,
                i.meta_json,
            )
            for i in instruments
        ]
        await self.db.executemany(sql, rows)
        await self.db.commit()
        return len(rows)

    async def list_instruments(self, venue: str | None = None) -> list[Instrument]:
        """列出已注册标的。"""
        if venue:
            cur = await self.db.execute(
                "SELECT * FROM instruments WHERE venue = ? ORDER BY instrument_id",
                (venue,),
            )
        else:
            cur = await self.db.execute(
                "SELECT * FROM instruments ORDER BY instrument_id"
            )
        rows = await cur.fetchall()
        return [Instrument(**dict(r)) for r in rows]

    async def get_watermark(
        self,
        venue: str,
        dataset: str,
        instrument_id: str,
        tf: str = "",
    ) -> int | None:
        """读取水位（毫秒）；无则返回 None。"""
        cur = await self.db.execute(
            """
            SELECT ts_ms FROM watermarks
            WHERE venue=? AND dataset=? AND instrument_id=? AND tf=?
            """,
            (venue, dataset, instrument_id, tf),
        )
        row = await cur.fetchone()
        return int(row["ts_ms"]) if row else None

    async def set_watermark(
        self,
        venue: str,
        dataset: str,
        instrument_id: str,
        ts_ms: int,
        tf: str = "",
    ) -> None:
        """推进水位（只增不减）。"""
        existing = await self.get_watermark(venue, dataset, instrument_id, tf)
        if existing is not None and ts_ms < existing:
            return
        await self.db.execute(
            """
            INSERT INTO watermarks (venue, dataset, instrument_id, tf, ts_ms)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(venue, dataset, instrument_id, tf) DO UPDATE SET
                ts_ms = excluded.ts_ms
            """,
            (venue, dataset, instrument_id, tf, ts_ms),
        )
        await self.db.commit()
