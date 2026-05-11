"""
SQLite persistence for error events using aiosqlite.
"""
import aiosqlite
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional
from app.models.event import ErrorEvent

DB_PATH = Path(os.getenv("SOFIA_DB_PATH", "data/sofia.db"))


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS error_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id  TEXT NOT NULL,
                service_name TEXT NOT NULL,
                level       TEXT NOT NULL,
                message     TEXT NOT NULL,
                detail      TEXT,
                traceback   TEXT,
                source      TEXT NOT NULL DEFAULT 'passive',
                timestamp   TEXT NOT NULL,
                notified    INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.commit()


async def insert_event(event: ErrorEvent) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        ts = (event.timestamp or datetime.utcnow()).isoformat()
        cursor = await db.execute(
            """INSERT INTO error_events
               (service_id, service_name, level, message, detail, traceback, source, timestamp, notified)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.service_id, event.service_name, event.level,
             event.message, event.detail, event.traceback,
             event.source, ts, int(event.notified)),
        )
        await db.commit()
        return cursor.lastrowid


async def get_events(
    service_id: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 200,
    since_hours: int = 24,
) -> List[ErrorEvent]:
    since = (datetime.utcnow() - timedelta(hours=since_hours)).isoformat()
    query = "SELECT * FROM error_events WHERE timestamp >= ?"
    params: list = [since]
    if service_id:
        query += " AND service_id = ?"
        params.append(service_id)
    if level:
        query += " AND level = ?"
        params.append(level)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [
            ErrorEvent(
                id=r["id"],
                service_id=r["service_id"],
                service_name=r["service_name"],
                level=r["level"],
                message=r["message"],
                detail=r["detail"],
                traceback=r["traceback"],
                source=r["source"],
                timestamp=datetime.fromisoformat(r["timestamp"]),
                notified=bool(r["notified"]),
            )
            for r in rows
        ]


async def purge_old_events(retention_days: int = 7):
    cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM error_events WHERE timestamp < ?", (cutoff,))
        await db.commit()


async def mark_notified(event_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE error_events SET notified=1 WHERE id=?", (event_id,))
        await db.commit()
