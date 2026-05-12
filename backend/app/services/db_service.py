"""
SQLite persistence for error events, restores, metrics and the alert queue.
Errors are grouped by fingerprint (service + level + message hash) like Sentry.
"""
import aiosqlite
import hashlib
import os
import statistics
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
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
                notified     INTEGER NOT NULL DEFAULT 0,
                tags         TEXT,
                environment  TEXT,
                release      TEXT
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
                breadcrumbs  TEXT,
                FOREIGN KEY(issue_id) REFERENCES issues(id)
            )
        """)
        # Service-level metrics (response times, status codes, uptime)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id   TEXT NOT NULL,
                timestamp    TEXT NOT NULL,
                response_ms  REAL,
                status_code  INTEGER,
                is_up        INTEGER NOT NULL DEFAULT 1
            )
        """)
        # Persisted restore history (in-memory store is reset on restart)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS restores (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id     TEXT NOT NULL,
                service_name   TEXT NOT NULL,
                status         TEXT NOT NULL,
                trigger_mode   TEXT NOT NULL DEFAULT 'manual',
                requested_at   TEXT NOT NULL,
                confirmed_at   TEXT,
                finished_at    TEXT,
                result_message TEXT,
                devin_output   TEXT,
                retry_count    INTEGER DEFAULT 0,
                restore_method TEXT
            )
        """)
        # WhatsApp alert queue (graceful degradation when WPP is down)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alert_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message     TEXT NOT NULL,
                phone       TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                sent_at     TEXT,
                attempts    INTEGER DEFAULT 0,
                last_error  TEXT
            )
        """)
        # Indexes for common query patterns
        await db.execute("CREATE INDEX IF NOT EXISTS idx_issues_service ON issues(service_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_issues_level ON issues(level)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_issues_last_seen ON issues(last_seen)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_issues_resolved ON issues(resolved)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_occ_issue ON occurrences(issue_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_occ_ts ON occurrences(timestamp)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_service ON metrics(service_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(timestamp)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_restores_service ON restores(service_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_restores_finished ON restores(finished_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_queue_sent ON alert_queue(sent_at)")
        # Migrate old schema if needed
        for stmt in (
            "ALTER TABLE issues ADD COLUMN url TEXT",
            "ALTER TABLE issues ADD COLUMN user_info TEXT",
            "ALTER TABLE issues ADD COLUMN tags TEXT",
            "ALTER TABLE issues ADD COLUMN environment TEXT",
            "ALTER TABLE issues ADD COLUMN release TEXT",
            "ALTER TABLE occurrences ADD COLUMN breadcrumbs TEXT",
        ):
            try:
                await db.execute(stmt)
            except Exception:
                pass
        await db.commit()


# ── Issues / occurrences ─────────────────────────────────────────────────────


async def upsert_event(event: ErrorEvent) -> Tuple[int, bool, bool]:
    """
    Insert or update an issue by fingerprint.
    Returns (issue_id, is_new, is_regression).

    is_regression is True when an issue that was previously marked resolved
    has a new occurrence.
    """
    fp = _fingerprint(event.service_id, event.level, event.message)
    ts = (event.timestamp or datetime.utcnow()).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT id, count, notified, resolved FROM issues WHERE fingerprint = ?", (fp,)
        )).fetchone()

        is_regression = False
        if row:
            # Update existing issue
            is_regression = bool(row["resolved"])
            await db.execute(
                """UPDATE issues SET
                   count = count + 1,
                   last_seen = ?,
                   detail = COALESCE(?, detail),
                   traceback = COALESCE(?, traceback),
                   url = COALESCE(?, url),
                   user_info = COALESCE(?, user_info),
                   tags = COALESCE(?, tags),
                   environment = COALESCE(?, environment),
                   release = COALESCE(?, release),
                   resolved = 0
                   WHERE fingerprint = ?""",
                (ts, event.detail, event.traceback, event.url, event.user_info,
                 event.tags, event.environment, event.release, fp),
            )
            issue_id = row["id"]
            is_new = False
        else:
            # New issue
            cursor = await db.execute(
                """INSERT INTO issues
                   (fingerprint, service_id, service_name, level, message,
                    detail, traceback, url, user_info, source,
                    first_seen, last_seen, tags, environment, release)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fp, event.service_id, event.service_name, event.level,
                 event.message, event.detail, event.traceback,
                 event.url, event.user_info, event.source, ts, ts,
                 event.tags, event.environment, event.release),
            )
            issue_id = cursor.lastrowid
            is_new = True

        # Always log raw occurrence (breadcrumbs passed separately via insert_occurrence)
        await db.execute(
            """INSERT INTO occurrences (issue_id, timestamp, url, user_info, detail, traceback)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (issue_id, ts, event.url, event.user_info, event.detail, event.traceback),
        )
        await db.commit()
        return issue_id, is_new, is_regression


async def insert_occurrence_breadcrumbs(issue_id: int, breadcrumbs_json: str) -> None:
    """Attach breadcrumbs (JSON string) to the most recent occurrence of issue_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            """UPDATE occurrences SET breadcrumbs = ?
               WHERE id = (SELECT id FROM occurrences WHERE issue_id = ?
                           ORDER BY id DESC LIMIT 1)""",
            (breadcrumbs_json, issue_id),
        )
        await db.commit()


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
        await db.execute("DELETE FROM occurrences WHERE timestamp < ?", (cutoff,))
        await db.execute("DELETE FROM issues WHERE last_seen < ? AND resolved = 1", (cutoff,))
        await db.execute("DELETE FROM metrics WHERE timestamp < ?", (cutoff,))
        await db.commit()


# ── backwards-compat shim used by health_service / log_service ───────────────


async def insert_event(event: ErrorEvent) -> int:
    issue_id, _, _ = await upsert_event(event)
    return issue_id


async def get_error_count(service_id: Optional[str] = None, window_minutes: int = 60) -> int:
    """Count occurrences of ERROR/CRITICAL issues in the time window."""
    since = (datetime.utcnow() - timedelta(minutes=window_minutes)).isoformat()
    query = (
        "SELECT COUNT(*) AS n FROM occurrences o "
        "JOIN issues i ON o.issue_id = i.id "
        "WHERE o.timestamp >= ? AND i.level IN ('ERROR','CRITICAL')"
    )
    params: list = [since]
    if service_id:
        query += " AND i.service_id = ?"
        params.append(service_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(query, params)).fetchone()
        return int(row["n"]) if row else 0


# ── Metrics ──────────────────────────────────────────────────────────────────


async def record_metric(
    service_id: str,
    response_ms: Optional[float],
    status_code: Optional[int],
    is_up: bool,
) -> None:
    ts = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT INTO metrics (service_id, timestamp, response_ms, status_code, is_up) "
            "VALUES (?, ?, ?, ?, ?)",
            (service_id, ts, response_ms, status_code, 1 if is_up else 0),
        )
        await db.commit()


async def get_metrics(service_id: str, since_hours: int = 24) -> List[dict]:
    since = (datetime.utcnow() - timedelta(hours=since_hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT timestamp, response_ms, status_code, is_up FROM metrics "
            "WHERE service_id = ? AND timestamp >= ? ORDER BY timestamp ASC",
            (service_id, since),
        )).fetchall()
        return [dict(r) for r in rows]


async def get_uptime_percent(service_id: str, since_hours: int = 24) -> float:
    since = (datetime.utcnow() - timedelta(hours=since_hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT COUNT(*) AS total, SUM(is_up) AS ups FROM metrics "
            "WHERE service_id = ? AND timestamp >= ?",
            (service_id, since),
        )).fetchone()
        total = int(row["total"] or 0)
        ups = int(row["ups"] or 0)
        if total == 0:
            return 100.0
        return round(ups / total * 100, 2)


async def get_response_stats(service_id: str, since_hours: int = 24) -> dict:
    metrics = await get_metrics(service_id, since_hours)
    times = [m["response_ms"] for m in metrics if m["response_ms"] is not None]
    if not times:
        return {
            "avg": None, "p50": None, "p95": None, "p99": None,
            "min": None, "max": None, "total_checks": len(metrics),
        }
    times_sorted = sorted(times)

    def _percentile(p: float) -> float:
        if not times_sorted:
            return 0.0
        k = max(0, min(len(times_sorted) - 1, int(round(p / 100 * (len(times_sorted) - 1)))))
        return round(times_sorted[k], 1)

    return {
        "avg": round(statistics.fmean(times), 1),
        "p50": _percentile(50),
        "p95": _percentile(95),
        "p99": _percentile(99),
        "min": round(min(times), 1),
        "max": round(max(times), 1),
        "total_checks": len(metrics),
    }


# ── Restores ─────────────────────────────────────────────────────────────────


async def save_restore(restore: dict) -> int:
    """Insert a new row in the restores table. Returns the new row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        cursor = await db.execute(
            """INSERT INTO restores
               (service_id, service_name, status, trigger_mode, requested_at,
                confirmed_at, finished_at, result_message, devin_output,
                retry_count, restore_method)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                restore.get("service_id"),
                restore.get("service_name"),
                restore.get("status"),
                restore.get("trigger_mode", "manual"),
                restore.get("requested_at"),
                restore.get("confirmed_at"),
                restore.get("finished_at"),
                restore.get("result_message"),
                restore.get("devin_output"),
                int(restore.get("retry_count", 0)),
                restore.get("restore_method"),
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def update_restore(restore_id: int, **fields) -> None:
    """Update arbitrary columns of a restore row."""
    if not fields:
        return
    allowed = {
        "status", "confirmed_at", "finished_at", "result_message",
        "devin_output", "retry_count", "restore_method", "trigger_mode",
    }
    sets = [f"{k} = ?" for k in fields if k in allowed]
    if not sets:
        return
    params = [fields[k] for k in fields if k in allowed] + [restore_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            f"UPDATE restores SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await db.commit()


async def get_restore_history(limit: int = 50, service_id: Optional[str] = None) -> List[dict]:
    query = "SELECT * FROM restores"
    params: list = []
    if service_id:
        query += " WHERE service_id = ?"
        params.append(service_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(query, params)).fetchall()
        return [dict(r) for r in rows]


# ── Alert queue ──────────────────────────────────────────────────────────────


async def enqueue_alert(phone: str, message: str) -> int:
    ts = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        cursor = await db.execute(
            "INSERT INTO alert_queue (phone, message, created_at) VALUES (?, ?, ?)",
            (phone, message, ts),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def get_pending_alerts(max_attempts: int = 10) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM alert_queue WHERE sent_at IS NULL AND attempts < ? ORDER BY id ASC",
            (max_attempts,),
        )).fetchall()
        return [dict(r) for r in rows]


async def mark_alert_sent(alert_id: int) -> None:
    ts = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "UPDATE alert_queue SET sent_at = ? WHERE id = ?",
            (ts, alert_id),
        )
        await db.commit()


async def mark_alert_failed(alert_id: int, error: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "UPDATE alert_queue SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            (error[:500], alert_id),
        )
        await db.commit()
