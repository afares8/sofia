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

DB_PATH = Path(os.getenv("SOFIA_DB_PATH", str(Path(__file__).resolve().parents[2] / "data" / "sofia.db")))


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
        # Nightly review reports
        await db.execute("""
            CREATE TABLE IF NOT EXISTS nightly_reports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                period_start    TEXT NOT NULL,
                period_end      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                issues_analyzed INTEGER NOT NULL DEFAULT 0,
                proposals       TEXT,
                approved_at     TEXT,
                rejected_at     TEXT,
                applied_at      TEXT,
                apply_output    TEXT,
                notes           TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_nightly_created ON nightly_reports(created_at)")
        # Per-proposal apply runs (one row per proposal per apply attempt)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS proposal_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id       INTEGER NOT NULL,
                proposal_index  INTEGER NOT NULL,
                issue_id        INTEGER,
                service_id      TEXT,
                title           TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                started_at      TEXT,
                finished_at     TEXT,
                duration_s      REAL,
                devin_output    TEXT,
                error_msg       TEXT,
                FOREIGN KEY (report_id) REFERENCES nightly_reports(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_prun_report ON proposal_runs(report_id)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                service_id      TEXT,
                issue_id        INTEGER,
                repo_id         TEXT,
                goal            TEXT NOT NULL,
                autonomy_level  INTEGER NOT NULL DEFAULT 1,
                mode            TEXT NOT NULL DEFAULT 'plan',
                sandbox_path    TEXT,
                base_branch     TEXT,
                work_branch     TEXT,
                branch_name     TEXT,
                commit_sha      TEXT,
                devin_output    TEXT,
                diff_summary    TEXT,
                tests_output    TEXT,
                tests_status    TEXT,
                smoke_output    TEXT,
                smoke_status    TEXT,
                verifier_output TEXT,
                verifier_status TEXT,
                verifier_decision TEXT,
                risk            TEXT,
                blocked_reason  TEXT,
                pr_url          TEXT,
                promoted_at     TEXT,
                result_message  TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_jobs_status ON ai_jobs(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_jobs_created ON ai_jobs(created_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS action_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                finished_at     TEXT,
                action_type     TEXT NOT NULL,
                service_id      TEXT,
                status          TEXT NOT NULL DEFAULT 'running',
                autonomy_level  INTEGER NOT NULL DEFAULT 1,
                trigger_source  TEXT,
                target          TEXT,
                output          TEXT,
                error_msg       TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_action_runs_created ON action_runs(created_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS github_sync_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                finished_at     TEXT,
                repo_id         TEXT NOT NULL,
                repo_path       TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'running',
                branch          TEXT,
                files_changed   INTEGER DEFAULT 0,
                commit_sha      TEXT,
                pushed          INTEGER DEFAULT 0,
                output          TEXT,
                error_msg       TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_github_sync_created ON github_sync_runs(created_at)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL,
                entity_type     TEXT NOT NULL,
                entity_id       INTEGER,
                event_type      TEXT NOT NULL,
                message         TEXT,
                data            TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id)")
        # Migrate old schema if needed
        for stmt in (
            "ALTER TABLE issues ADD COLUMN url TEXT",
            "ALTER TABLE issues ADD COLUMN user_info TEXT",
            "ALTER TABLE issues ADD COLUMN tags TEXT",
            "ALTER TABLE issues ADD COLUMN environment TEXT",
            "ALTER TABLE issues ADD COLUMN release TEXT",
            "ALTER TABLE occurrences ADD COLUMN breadcrumbs TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN repo_id TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN sandbox_path TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN base_branch TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN work_branch TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN commit_sha TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN tests_status TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN smoke_output TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN smoke_status TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN verifier_decision TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN pr_url TEXT",
            "ALTER TABLE ai_jobs ADD COLUMN promoted_at TEXT",
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


# ── Nightly reports ──────────────────────────────────────────────────────────


async def save_nightly_report(
    period_start: str,
    period_end: str,
    issues_analyzed: int,
    proposals: str,
) -> int:
    ts = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        cursor = await db.execute(
            """INSERT INTO nightly_reports
               (created_at, period_start, period_end, status, issues_analyzed, proposals)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (ts, period_start, period_end, issues_analyzed, proposals),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def get_nightly_reports(limit: int = 30) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM nightly_reports ORDER BY id DESC LIMIT ?", (limit,)
        )).fetchall()
        return [dict(r) for r in rows]


async def get_nightly_report(report_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM nightly_reports WHERE id = ?", (report_id,)
        )).fetchone()
        return dict(row) if row else None


async def update_nightly_report(report_id: int, **fields) -> None:
    allowed = {"status", "approved_at", "rejected_at", "applied_at", "apply_output", "notes"}
    sets = [f"{k} = ?" for k in fields if k in allowed]
    if not sets:
        return
    params = [fields[k] for k in fields if k in allowed] + [report_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            f"UPDATE nightly_reports SET {', '.join(sets)} WHERE id = ?", params
        )
        await db.commit()


# ── proposal_runs CRUD ────────────────────────────────────────────────────────

async def create_proposal_run(
    report_id: int,
    proposal_index: int,
    issue_id: Optional[int],
    service_id: Optional[str],
    title: Optional[str],
) -> int:
    """Create a new proposal_run row in 'running' state. Returns its id."""
    started = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        cursor = await db.execute(
            """INSERT INTO proposal_runs
               (report_id, proposal_index, issue_id, service_id, title, status, started_at)
               VALUES (?, ?, ?, ?, ?, 'running', ?)""",
            (report_id, proposal_index, issue_id, service_id, title, started),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def finish_proposal_run(
    run_id: int,
    success: bool,
    devin_output: str,
    error_msg: Optional[str] = None,
    duration_s: Optional[float] = None,
) -> None:
    finished = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            """UPDATE proposal_runs
               SET status = ?, finished_at = ?, duration_s = ?,
                   devin_output = ?, error_msg = ?
               WHERE id = ?""",
            (
                "success" if success else "failed",
                finished,
                duration_s,
                devin_output[:16000] if devin_output else None,
                error_msg,
                run_id,
            ),
        )
        await db.commit()


async def get_proposal_runs(report_id: int) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM proposal_runs WHERE report_id = ? ORDER BY id DESC",
            (report_id,),
        )).fetchall()
        return [dict(r) for r in rows]


async def resolve_issue(issue_id: int) -> None:
    """Mark an issue as resolved."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "UPDATE issues SET resolved = 1 WHERE id = ?", (issue_id,)
        )
        await db.commit()


async def create_ai_job(
    goal: str,
    service_id: Optional[str] = None,
    issue_id: Optional[int] = None,
    autonomy_level: int = 1,
    mode: str = "plan",
    repo_id: Optional[str] = None,
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        cursor = await db.execute(
            """INSERT INTO ai_jobs
               (created_at, updated_at, status, service_id, issue_id, repo_id, goal, autonomy_level, mode)
               VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (now, now, service_id, issue_id, repo_id, goal, autonomy_level, mode),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def get_ai_jobs(limit: int = 50) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM ai_jobs ORDER BY id DESC LIMIT ?", (limit,)
        )).fetchall()
        return [dict(r) for r in rows]


async def get_ai_job(job_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM ai_jobs WHERE id = ?", (job_id,)
        )).fetchone()
        return dict(row) if row else None


async def update_ai_job(job_id: int, **fields) -> None:
    allowed = {
        "status", "repo_id", "sandbox_path", "base_branch", "work_branch", "branch_name",
        "commit_sha", "devin_output", "diff_summary", "tests_output", "tests_status",
        "smoke_output", "smoke_status", "verifier_output", "verifier_status",
        "verifier_decision", "risk", "blocked_reason", "pr_url", "promoted_at",
        "result_message",
    }
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return
    clean["updated_at"] = datetime.utcnow().isoformat()
    sets = [f"{k} = ?" for k in clean]
    params = [clean[k] for k in clean] + [job_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(f"UPDATE ai_jobs SET {', '.join(sets)} WHERE id = ?", params)
        await db.commit()


async def get_open_ai_job_for_issue(issue_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            """SELECT * FROM ai_jobs
               WHERE issue_id = ? AND status IN ('pending', 'running', 'verified', 'completed')
               ORDER BY id DESC LIMIT 1""",
            (issue_id,),
        )).fetchone()
        return dict(row) if row else None


async def create_action_run(
    action_type: str,
    service_id: Optional[str] = None,
    autonomy_level: int = 1,
    trigger_source: Optional[str] = None,
    target: Optional[str] = None,
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        cursor = await db.execute(
            """INSERT INTO action_runs
               (created_at, action_type, service_id, status, autonomy_level, trigger_source, target)
               VALUES (?, ?, ?, 'running', ?, ?, ?)""",
            (now, action_type, service_id, autonomy_level, trigger_source, target),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def finish_action_run(
    run_id: int,
    status: str,
    output: Optional[str] = None,
    error_msg: Optional[str] = None,
) -> None:
    finished = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            """UPDATE action_runs
               SET status = ?, finished_at = ?, output = ?, error_msg = ?
               WHERE id = ?""",
            (status, finished, output[:16000] if output else None, error_msg, run_id),
        )
        await db.commit()


async def get_action_runs(limit: int = 50) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM action_runs ORDER BY id DESC LIMIT ?", (limit,)
        )).fetchall()
        return [dict(r) for r in rows]


async def create_github_sync_run(repo_id: str, repo_path: str, branch: str) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        cursor = await db.execute(
            """INSERT INTO github_sync_runs
               (created_at, repo_id, repo_path, status, branch)
               VALUES (?, ?, ?, 'running', ?)""",
            (now, repo_id, repo_path, branch),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def finish_github_sync_run(
    run_id: int,
    status: str,
    files_changed: int = 0,
    commit_sha: Optional[str] = None,
    pushed: bool = False,
    output: Optional[str] = None,
    error_msg: Optional[str] = None,
) -> None:
    finished = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            """UPDATE github_sync_runs
               SET status = ?, finished_at = ?, files_changed = ?, commit_sha = ?,
                   pushed = ?, output = ?, error_msg = ?
               WHERE id = ?""",
            (
                status,
                finished,
                files_changed,
                commit_sha,
                int(pushed),
                output[:16000] if output else None,
                error_msg,
                run_id,
            ),
        )
        await db.commit()


async def get_github_sync_runs(limit: int = 50) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM github_sync_runs ORDER BY id DESC LIMIT ?", (limit,)
        )).fetchall()
        return [dict(r) for r in rows]


async def add_audit_event(
    entity_type: str,
    event_type: str,
    entity_id: Optional[int] = None,
    message: Optional[str] = None,
    data: Optional[str] = None,
) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        cursor = await db.execute(
            """INSERT INTO audit_events
               (created_at, entity_type, entity_id, event_type, message, data)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (now, entity_type, entity_id, event_type, message, data),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def get_audit_events(
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    limit: int = 100,
) -> List[dict]:
    query = "SELECT * FROM audit_events WHERE 1=1"
    params: list = []
    if entity_type:
        query += " AND entity_type = ?"
        params.append(entity_type)
    if entity_id is not None:
        query += " AND entity_id = ?"
        params.append(entity_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(query, params)).fetchall()
        return [dict(r) for r in rows]


async def get_activity_counts(since_hours: int = 24) -> dict:
    since = (datetime.utcnow() - timedelta(hours=since_hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row

        def scalar(query: str, params: tuple = ()) -> int:
            raise RuntimeError("sync placeholder")

        async def count(query: str, params: tuple = ()) -> int:
            row = await (await db.execute(query, params)).fetchone()
            return int(row[0] or 0) if row else 0

        issue_rows = await (await db.execute(
            """SELECT service_id, level, COUNT(*) AS issues, COALESCE(SUM(count), 0) AS occurrences
               FROM issues WHERE last_seen >= ?
               GROUP BY service_id, level ORDER BY occurrences DESC""",
            (since,),
        )).fetchall()
        restore_rows = await (await db.execute(
            """SELECT status, COUNT(*) AS count FROM restores
               WHERE requested_at >= ? GROUP BY status""",
            (since,),
        )).fetchall()
        job_rows = await (await db.execute(
            """SELECT status, COUNT(*) AS count FROM ai_jobs
               WHERE created_at >= ? GROUP BY status""",
            (since,),
        )).fetchall()
        action_rows = await (await db.execute(
            """SELECT status, action_type, COUNT(*) AS count FROM action_runs
               WHERE created_at >= ? GROUP BY status, action_type""",
            (since,),
        )).fetchall()
        sync_rows = await (await db.execute(
            """SELECT status, COUNT(*) AS count FROM github_sync_runs
               WHERE created_at >= ? GROUP BY status""",
            (since,),
        )).fetchall()
        audit_rows = await (await db.execute(
            """SELECT event_type, COUNT(*) AS count FROM audit_events
               WHERE created_at >= ? GROUP BY event_type ORDER BY count DESC LIMIT 10""",
            (since,),
        )).fetchall()

        return {
            "since": since,
            "issues_total": await count("SELECT COUNT(*) FROM issues WHERE last_seen >= ?", (since,)),
            "occurrences_total": await count("SELECT COALESCE(SUM(count), 0) FROM issues WHERE last_seen >= ?", (since,)),
            "unresolved_total": await count("SELECT COUNT(*) FROM issues WHERE resolved = 0"),
            "critical_open": await count("SELECT COUNT(*) FROM issues WHERE resolved = 0 AND level = 'CRITICAL'"),
            "error_open": await count("SELECT COUNT(*) FROM issues WHERE resolved = 0 AND level = 'ERROR'"),
            "metrics_total": await count("SELECT COUNT(*) FROM metrics WHERE timestamp >= ?", (since,)),
            "queued_alerts_pending": await count("SELECT COUNT(*) FROM alert_queue WHERE sent_at IS NULL"),
            "nightly_reports": await count("SELECT COUNT(*) FROM nightly_reports WHERE created_at >= ?", (since,)),
            "proposal_runs": await count("SELECT COUNT(*) FROM proposal_runs WHERE started_at >= ?", (since,)),
            "issues_by_service": [dict(r) for r in issue_rows],
            "restores_by_status": [dict(r) for r in restore_rows],
            "jobs_by_status": [dict(r) for r in job_rows],
            "actions_by_status": [dict(r) for r in action_rows],
            "github_sync_by_status": [dict(r) for r in sync_rows],
            "audit_events": [dict(r) for r in audit_rows],
        }
