"""
SQLite persistence for error events using aiosqlite.
Errors are grouped by fingerprint (service + level + message hash) like Sentry.
"""
import aiosqlite
import hashlib
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional
from app.models.event import ErrorEvent

DB_PATH = Path(os.getenv("SOFIA_DB_PATH", "data/sofia.db"))


def _fingerprint(service_id: str, level: str, message: str) -> str:
    """Stable hash to group identical errors together."""
    # Normalize message: strip memory addresses, line numbers, UUIDs
    import re
    msg = message[:300]
    msg = re.sub(r'0x[0-9a-fA-F]+', '0xADDR', msg)
    msg = re.sub(r'\b\d{4,}\b', 'N', msg)           # long numbers
    msg = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', 'UUID', msg)
    key = f"{service_id}:{level}:{msg}"
    return hashlib.md5(key.encode()).hexdigest()


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        # Main grouped issues table (like Sentry "issues")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS issues (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint  TEXT NOT NULL UNIQUE,
                service_id   TEXT NOT NULL,
                service_name TEXT NOT NULL,
                level        TEXT NOT NULL,
                message      TEXT NOT NULL,
                detail       TEXT,
                traceback    TEXT,
                url          TEXT,
                user_info    TEXT,
                source       TEXT NOT NULL DEFAULT 'active',
                count        INTEGER NOT NULL DEFAULT 1,
                first_seen   TEXT NOT NULL,
                last_seen    TEXT NOT NULL,
                resolved     INTEGER NOT NULL DEFAULT 0,
                notified     INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Raw occurrences log
        await db.execute("""
            CREATE TABLE IF NOT EXISTS occurrences (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id     INTEGER NOT NULL,
                timestamp    TEXT NOT NULL,
                url          TEXT,
                user_info    TEXT,
                detail       TEXT,
                traceback    TEXT,
                FOREIGN KEY(issue_id) REFERENCES issues(id)
            )
        """)
        # Indexes for common query patterns
        await db.execute("CREATE INDEX IF NOT EXISTS idx_issues_service ON issues(service_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_issues_level ON issues(level)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_issues_last_seen ON issues(last_seen)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_issues_resolved ON issues(resolved)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_occ_issue ON occurrences(issue_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_occ_ts ON occurrences(timestamp)")
        # Migrate old schema if needed
        try:
            await db.execute("ALTER TABLE issues ADD COLUMN url TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE issues ADD COLUMN user_info TEXT")
        except Exception:
            pass
        await db.commit()


async def upsert_event(event: ErrorEvent) -> tuple[int, bool]:
    """
    Insert or update an issue by fingerprint.
    Returns (issue_id, is_new).
    """
    fp = _fingerprint(event.service_id, event.level, event.message)
    ts = (event.timestamp or datetime.utcnow()).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT id, count, notified FROM issues WHERE fingerprint = ?", (fp,)
        )).fetchone()

        if row:
            # Update existing issue
            await db.execute(
                """UPDATE issues SET
                   count = count + 1,
                   last_seen = ?,
                   detail = COALESCE(?, detail),
                   traceback = COALESCE(?, traceback),
                   url = COALESCE(?, url),
                   user_info = COALESCE(?, user_info),
                   resolved = 0
                   WHERE fingerprint = ?""",
                (ts, event.detail, event.traceback, event.url, event.user_info, fp),
            )
            issue_id = row["id"]
            is_new = False
        else:
            # New issue
            cursor = await db.execute(
                """INSERT INTO issues
                   (fingerprint, service_id, service_name, level, message,
                    detail, traceback, url, user_info, source, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fp, event.service_id, event.service_name, event.level,
                 event.message, event.detail, event.traceback,
                 event.url, event.user_info, event.source, ts, ts),
            )
            issue_id = cursor.lastrowid
            is_new = True

        # Always log raw occurrence
        await db.execute(
            """INSERT INTO occurrences (issue_id, timestamp, url, user_info, detail, traceback)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (issue_id, ts, event.url, event.user_info, event.detail, event.traceback),
        )
        await db.commit()
        return issue_id, is_new


async def get_issues(
    service_id: Optional[str] = None,
    level: Optional[str] = None,
    resolved: bool = False,
    limit: int = 200,
    since_hours: int = 24 * 7,
) -> List[dict]:
    since = (datetime.utcnow() - timedelta(hours=since_hours)).isoformat()
    query = "SELECT * FROM issues WHERE last_seen >= ? AND resolved = ?"
    params: list = [since, int(resolved)]
    if service_id:
        query += " AND service_id = ?"
        params.append(service_id)
    if level:
        query += " AND level = ?"
        params.append(level)
    query += " ORDER BY last_seen DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(query, params)).fetchall()
        return [dict(r) for r in rows]


async def get_occurrences(issue_id: int, limit: int = 50) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM occurrences WHERE issue_id = ? ORDER BY timestamp DESC LIMIT ?",
            (issue_id, limit)
        )).fetchall()
        return [dict(r) for r in rows]


async def resolve_issue(issue_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("UPDATE issues SET resolved = 1 WHERE id = ?", (issue_id,))
        await db.commit()


async def mark_notified(issue_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("UPDATE issues SET notified = 1 WHERE id = ?", (issue_id,))
        await db.commit()


async def purge_old_events(retention_days: int = 7):
    cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        async with db.execute("BEGIN"):
            pass
        await db.execute("DELETE FROM occurrences WHERE timestamp < ?", (cutoff,))
        await db.execute("DELETE FROM issues WHERE last_seen < ? AND resolved = 1", (cutoff,))
        await db.commit()


# ── backwards-compat shim used by health_service ──────────────────────────────
async def insert_event(event: ErrorEvent) -> int:
    issue_id, _ = await upsert_event(event)
    return issue_id
