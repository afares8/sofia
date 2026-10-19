"""
Droplet docker monitor - scrapes remote container logs, lifecycle events and
host-level signals over SSH. Runs entirely on SERVER-SOFIA (LocalSystem).

Design notes
------------
- Uses paramiko (in-process SSH) — no ssh.exe key-ownership checks; SYSTEM
  already has read access to the user's ed25519 key.
- NOTHING is installed or modified on the droplet: every poll opens one short
  SSH session and runs read-only `docker ps`, `docker logs`, `docker events`,
  `dmesg`, `df`, `free`.
- Per-container remote `grep` keeps transfer bounded AND never drops an error
  line under volume (a plain `tail` cap would cut errors off the window when a
  container logs heavily — e.g. postgres statement logging).
- Dedup is persisted to data/droplet_state.json (high-water timestamps,
  bounded seen-hashes, last health status) so a Sofia restart does not
  re-ingest the same lines.
- Classification happens locally per log flavor (structlog JSON, nginx,
  postgres, generic) with a WARNING tier that is stored but not alerted.

Env knobs (backend/.env):
  SOFIA_DROPLET_MONITOR        true/false               (default true)
  SOFIA_DROPLET_HOST           droplet IP               (default 198.211.99.4)
  SOFIA_DROPLET_USER           ssh user                 (default root)
  SOFIA_DROPLET_KEY            private key path         (default ~ahmed/.ssh/id_ed25519)
  SOFIA_DROPLET_POLL_S         seconds between polls    (default 120)
  SOFIA_DROPLET_BACKFILL_MIN   first-poll lookback, min (default 15)
  SOFIA_DROPLET_DISK_WARN_PCT  disk usage warning level (default 85)
  SOFIA_DROPLET_MEM_WARN_PCT   min free-mem warning pct (default 10)
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

from app.models.event import ErrorEvent
from app.services import db_service, whatsapp_service
from app.services.config_service import load_config

logger = logging.getLogger("sofia.docker")

ENABLED = os.getenv("SOFIA_DROPLET_MONITOR", "true").lower() in ("1", "true", "yes")
SSH_HOST = os.getenv("SOFIA_DROPLET_HOST", "198.211.99.4")
SSH_USER = os.getenv("SOFIA_DROPLET_USER", "root")
SSH_KEY = os.getenv("SOFIA_DROPLET_KEY", r"C:\Users\ahmed\.ssh\id_ed25519")
POLL_SECONDS = int(os.getenv("SOFIA_DROPLET_POLL_S", "120"))
BACKFILL_MINUTES = int(os.getenv("SOFIA_DROPLET_BACKFILL_MIN", "15"))
DISK_WARN_PCT = int(os.getenv("SOFIA_DROPLET_DISK_WARN_PCT", "85"))
MEM_WARN_PCT = int(os.getenv("SOFIA_DROPLET_MEM_WARN_PCT", "10"))

FETCH_TIMEOUT_S = 90          # hard cap for a whole SSH fetch round
GREP_TAIL_CAP = 1000          # safety bound AFTER remote grep (error lines only)
LOG_TAIL_CAP = 50000          # lineas maximas que docker emite por contenedor:
# `docker logs --since` sobre un json-log de varios GB (tec-db-1 ~2.2 GB)
# escanea el archivo hacia atras y supera cualquier timeout de canal; con
# --tail la emision queda acotada y la consulta baja de ~60-150 s a ~3 s.
SEEN_CAP = 3000               # persisted dedup ring size
STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "droplet_state.json"

# Container name -> (service_id, display name) shown in the dashboard.
# Containers NOT in this map are still scraped and land under 'droplet-<name>'.
CONTAINER_SERVICE_MAP = {
    "tec-backend-1":  ("tec", "ElenaTec Backend (DO)"),
    "tec-frontend-1": ("tec-frontend", "ElenaTec Frontend (DO)"),
    "tec-db-1":       ("tec-db", "ElenaTec DB Postgres"),
    "tec-redis-1":    ("tec-redis", "ElenaTec Redis"),
    "tec-backup-1":   ("tec-backup", "ElenaTec Backup"),
}

# ── remote-side grep patterns (superset of the local classifiers) ────────────
# Filtering on the droplet is what makes this production-safe: postgres logs
# every statement (thousands of lines per window) and a naive tail cap would
# silently drop real error lines from the beginning of the window.
# NOTE: these run through `grep -aE` on the droplet — POSIX ERE only.
# GNU grep -E does NOT support \s (it means a literal 's'), so use spaces.
_REMOTE_GREP = {
    "tec-backend-1":  r'"level" *: *"(warning|error|critical|fatal)"|Traceback|ERROR:',
    "tec-frontend-1": r"\[(warn|error|crit|alert|emerg)\]",
    "tec-db-1":       r"(ERROR|FATAL|PANIC):",
}
_REMOTE_GREP_DEFAULT = (
    r"(ERROR|CRITICAL|FATAL|PANIC|WARN|WARNING|Traceback|Exception"
    r"|oom-kill|Killed process)"
)

# ── local classifiers ────────────────────────────────────────────────────────
_JSON_WARN_RE = re.compile(r'"level"\s*:\s*"warning"', re.I)
_JSON_ERR_RE = re.compile(r'"level"\s*:\s*"error"', re.I)
_JSON_CRIT_RE = re.compile(r'"level"\s*:\s*"(critical|fatal)"', re.I)
_NGINX_WARN_RE = re.compile(r"\[warn\]", re.I)
_NGINX_ERR_RE = re.compile(r"\[error\]", re.I)
_NGINX_CRIT_RE = re.compile(r"\[(crit|alert|emerg)\]", re.I)
_NGINX_EXPECTED_ASSET_RE = re.compile(
    r"/assets/.*(?:is not found|no such file(?: or directory)?)"
    r"|directory index of .*/assets/ is forbidden",
    re.I,
)
_PG_ERR_RE = re.compile(r"\bERROR:")
_PG_CRIT_RE = re.compile(r"\b(FATAL|PANIC):")
_PG_EXPECTED_WARNING_RE = re.compile(
    r'\bFATAL:\s*(?:role\s+"postgres"\s+does not exist|connection to client lost)\b',
    re.I,
)
_GENERIC_WARN_RE = re.compile(r"\b(WARN|WARNING)\b")
_GENERIC_ERR_RE = re.compile(r"\b(ERROR|Traceback|Exception)\b")
_GENERIC_CRIT_RE = re.compile(r"\b(CRITICAL|FATAL|PANIC|oom-kill|Killed process)\b")
# JSON/non-error lines that only LOOK error-ish (zero counters, column names)
_NOISE_RE = re.compile(
    r'"[a-z_]*(?:failed|error|errors)[a-z_]*"\s*:\s*(0|null|false)\b'
    r"|\b(last_error|error_message|error_code|error_details)\s*(,|=|\))",
    re.I,
)
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+)\s+(.*)$")
# leading per-line timestamp inside the log body (nginx `YYYY/MM/DD HH:MM:SS`
# or postgres `YYYY-MM-DD HH:MM:SS UTC`) — strip from stored message so the
# fingerprint groups repeats of the same error into ONE issue.
_INLINE_TS_RE = re.compile(
    r"^\d{4}[/-]\d{2}[/-]\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?\s*(UTC|[+-]\d{4})?\s*")


def _classify(container: str, line: str) -> Optional[str]:
    """Return 'WARNING'/'ERROR'/'CRITICAL' for error-ish lines, else None."""
    if container == "tec-backend-1":
        if _JSON_CRIT_RE.search(line):
            return "CRITICAL"
        if _JSON_ERR_RE.search(line):
            return "ERROR"
        if _JSON_WARN_RE.search(line):
            return "WARNING"
        # non-JSON lines (uvicorn access, raw tracebacks, stderr)
        if _GENERIC_CRIT_RE.search(line):
            return "CRITICAL"
        if _GENERIC_ERR_RE.search(line):
            return "ERROR"
        return None
    if container == "tec-frontend-1":
        if _NGINX_CRIT_RE.search(line):
            return "CRITICAL"
        if _NGINX_ERR_RE.search(line):
            if _NGINX_EXPECTED_ASSET_RE.search(line):
                return "WARNING"
            return "ERROR"
        if _NGINX_WARN_RE.search(line):
            return "WARNING"
        return None
    if container == "tec-db-1":
        if _NOISE_RE.search(line):
            return None
        if _PG_EXPECTED_WARNING_RE.search(line):
            return "WARNING"
        if _PG_CRIT_RE.search(line):
            return "CRITICAL"
        if _PG_ERR_RE.search(line):
            return "ERROR"
        return None
    if _NOISE_RE.search(line):
        return None
    if _GENERIC_CRIT_RE.search(line):
        return "CRITICAL"
    if _GENERIC_ERR_RE.search(line):
        return "ERROR"
    if _GENERIC_WARN_RE.search(line):
        return "WARNING"
    return None


# ── persisted state ──────────────────────────────────────────────────────────
_last_ts: Dict[str, str] = {}
_health: Dict[str, str] = {}
_last_success_iso: Optional[str] = None   # utc iso of last successful poll
_seen: Deque[str] = deque(maxlen=SEEN_CAP)
_seen_set: Set[str] = set()
_oom_seen: Deque[str] = deque(maxlen=500)
_oom_seen_set: Set[str] = set()
_host_flags: Dict[str, bool] = {}
_status: Dict = {
    "enabled": ENABLED,
    "last_poll": None,
    "last_error": None,
    "last_ingest_count": 0,
    "consecutive_failures": 0,
    "log_failures": {},
}


def get_monitor_status() -> Dict:
    return dict(_status)


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def _load_state() -> None:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        global _last_success_iso
        _last_ts.update(raw.get("last_ts") or {})
        _health.update(raw.get("health") or {})
        _host_flags.update(raw.get("host_flags") or {})
        _last_success_iso = raw.get("last_success")
        for item in raw.get("seen") or []:
            _seen.append(item)
            _seen_set.add(item)
        for item in raw.get("oom") or []:
            _oom_seen.append(item)
            _oom_seen_set.add(item)
        logger.info(
            f"[DOCKER] State loaded ({len(_last_ts)} containers, "
            f"{len(_seen_set)} seen hashes).")
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"[DOCKER] Could not load state file: {exc}")


def _save_state() -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_ts": _last_ts,
            "health": _health,
            "host_flags": _host_flags,
            "last_success": _last_success_iso,
            "seen": list(_seen)[-SEEN_CAP:],
            "oom": list(_oom_seen)[-500:],
        }
        with tempfile.NamedTemporaryFile(
            "w", dir=str(STATE_PATH.parent), delete=False,
            encoding="utf-8", suffix=".tmp",
        ) as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name
        os.replace(tmp_path, STATE_PATH)
    except Exception as exc:
        logger.debug(f"[DOCKER] Could not save state file: {exc}")


def _mark_seen(container: str, line: str) -> bool:
    """True if this exact line was already processed (bounded ring dedup)."""
    key = f"{container}|{_sha(line)}"
    if key in _seen_set:
        return True
    _seen.append(key)
    _seen_set.add(key)
    if len(_seen_set) > SEEN_CAP:
        stale = _seen.popleft()
        _seen_set.discard(stale)
    return False


def _is_dup(container: str, ts: str, line: str) -> bool:
    if ts and ts <= _last_ts.get(container, ""):
        return True
    return _mark_seen(container, line)


# ── SSH fetch (sync — runs in a thread via asyncio.to_thread) ────────────────
def _ssh_exec(client, cmd: str, timeout: int = 40) -> str:
    _, stdout, _ = client.exec_command(cmd, timeout=timeout)
    return stdout.read().decode("utf-8", errors="replace")


def _remote_grep_pattern(container: str) -> str:
    return _REMOTE_GREP.get(container, _REMOTE_GREP_DEFAULT)


def _fetch_container_logs(client, names: List[str], since: str,
                          exec_fn=None) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Fetch + grep remoto de logs, aislado por contenedor.

    Un contenedor lento (json-log de varios GB) o caido no debe tumbar el
    poll completo: se omite esa fuente en esta pasada, el resto se ingesta
    igual y la falla queda en (failures) para diagnostico.
    """
    exec_fn = exec_fn or _ssh_exec
    logs: Dict[str, str] = {}
    failures: List[Tuple[str, str]] = []
    for name in names:
        pattern = _remote_grep_pattern(name)
        try:
            logs[name] = exec_fn(
                client,
                f"docker logs --since {since} --tail {LOG_TAIL_CAP} "
                f"--timestamps {name} 2>&1 "
                f"| grep -aE '{pattern}' | tail -{GREP_TAIL_CAP}",
            )
        except Exception as exc:
            failures.append((name, f"{type(exc).__name__}: {exc}"))
    return logs, failures


def _fetch_all(host: str, user: str, key_path: str, since: str) -> Dict:
    """
    One SSH session, all read-only commands. Returns a dict:
      ps        -> raw `docker ps -a` names|status lines
      logs      -> {container: grepped log text}
      events    -> raw docker events lines
      host      -> {oom_lines, disk_pct, mem_avail_pct}
    """
    import paramiko
    import time as _time

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # "Error reading SSH protocol banner" is transient when the droplet is
    # under memory/CPU pressure — retry once after a short pause before
    # declaring the poll failed.
    last_exc = None
    for attempt in range(2):
        try:
            client.connect(
                hostname=host, username=user, key_filename=key_path,
                timeout=10, banner_timeout=30, auth_timeout=20,
                look_for_keys=False, allow_agent=False,
            )
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                _time.sleep(5)
    if last_exc:
        raise last_exc
    try:
        ps = _ssh_exec(
            client, "docker ps -a --format '{{.Names}}|{{.Status}}'")
        names = [
            ln.split("|", 1)[0].strip()
            for ln in ps.splitlines() if ln.strip()
        ]

        logs, log_failures = _fetch_container_logs(client, names, since)

        events = _ssh_exec(
            client,
            "docker events --since " + since + " --until 0s "
            "--filter type=container "
            "--filter event=die --filter event=oom --filter event=kill "
            "--filter event=stop --filter event=start --filter event=restart "
            "--format '{{.Time}}|{{.Action}}|{{.Actor.Attributes.name}}"
            "|{{.Actor.Attributes.exitCode}}'",
        )

        oom = _ssh_exec(
            client,
            "dmesg -T 2>/dev/null | grep -iE 'Out of memory: Killed process"
            "|invoked oom-killer' | tail -50",
        )
        usage = _ssh_exec(
            client,
            "df -P / | awk 'NR==2{gsub(/%/,\"\",$5); print \"disk=\"$5}'; "
            "free -m | awk '/^Mem:/{print \"mem=\"int($7*100/$2)}'",
        )
        host_info = {"oom_lines": oom}
        for ln in usage.splitlines():
            if ln.startswith("disk="):
                host_info["disk_pct"] = int(ln[5:])
            elif ln.startswith("mem="):
                host_info["mem_avail_pct"] = int(ln[4:])

        # ── per-service health checks (all read-only) ───────────────────
        # Covers: pg availability/locks/connections, redis ping, backup
        # staleness (file mtime), certbot renewal failures, nginx 5xx burst.
        checks_raw = _ssh_exec(
            client,
            "echo pg_ready=$(docker exec tec-db-1 pg_isready -U magnate "
            "2>/dev/null || echo DOWN); "
            "echo pg_locks=$(docker exec tec-db-1 psql -U magnate -d postgres "
            "-tAc \"SELECT count(*) FROM pg_stat_activity WHERE "
            "wait_event_type='Lock' AND now()-query_start > interval '60 seconds'\" "
            "2>/dev/null || echo -1); "
            "echo pg_conn_pct=$(docker exec tec-db-1 psql -U magnate -d postgres "
            "-tAc \"SELECT round(100.0*count(*)/"
            "current_setting('max_connections')::int) FROM pg_stat_activity\" "
            "2>/dev/null || echo -1); "
            "echo redis_ping=$(docker exec tec-redis-1 redis-cli ping "
            "2>/dev/null || echo DOWN); "
            "echo backup_epoch=$(docker exec tec-backup-1 sh -c "
            "'f=$(ls -t /backups/*.tar.gz 2>/dev/null | head -1); "
            "[ -n \"$f\" ] && stat -c %Y \"$f\" || echo 0' 2>/dev/null); "
            "echo certbot_state=$(systemctl is-failed certbot.service "
            "2>/dev/null || echo unknown); "
            "echo certbot_errs=$(journalctl -u certbot.service --since -48h "
            "--no-pager -q 2>/dev/null | grep -ciE 'error|failed'); "
            "echo nginx5xx=$(docker logs tec-frontend-1 --since " + since +
            " 2>/dev/null | grep -cE '\" 5[0-9][0-9] ')",
            timeout=60,
        )
        checks = {}
        for ln in checks_raw.splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                checks[k.strip()] = v.strip()

        return {"ps": ps, "logs": logs, "events": events,
                "host": host_info, "checks": checks,
                "log_failures": log_failures}
    finally:
        client.close()


# ── event ingestion ──────────────────────────────────────────────────────────
async def _store_and_alert(event: ErrorEvent) -> None:
    issue_id, is_new, is_regression = await db_service.upsert_event(event)
    if event.level in ("ERROR", "CRITICAL") and (is_new or is_regression):
        cfg = load_config()
        prefix = "🔄 REGRESIÓN: " if is_regression else ""
        sent = await whatsapp_service.send_alert(
            cfg.alerts, event.service_name, event.service_id,
            event.level, prefix + event.message[:200])
        if sent:
            await db_service.mark_notified(issue_id)


async def _ingest_log_line(container: str, ts: str, line: str) -> bool:
    service_id, service_name = CONTAINER_SERVICE_MAP.get(
        container, ("droplet-" + container, "DO " + container))
    level = _classify(container, line)
    if not level or _is_dup(container, ts, line):
        return False
    if ts and ts > _last_ts.get(container, ""):
        _last_ts[container] = ts
    # strip the inner log timestamp + nginx worker/request-id so identical
    # errors share one fingerprint (grouped into a single issue)
    message = _INLINE_TS_RE.sub("", line)
    message = re.sub(r"\d+#\d+: \*\d+ ", "", message)
    await _store_and_alert(ErrorEvent(
        service_id=service_id,
        service_name=service_name,
        level=level,
        message=message[:500],
        detail=f"container={container} ts={ts}",
        source="docker",
        timestamp=datetime.utcnow(),
    ))
    return True


# Lifecycle events that are operator actions (deploys) vs real failures.
_LIFECYCLE_CLEAN_EXITS = {0, 143}          # clean stop / SIGTERM
_LIFECYCLE_LEVELS = {
    "oom": "CRITICAL",
    "kill": "WARNING",
    "stop": "WARNING",
    "start": "INFO",
    "restart": "WARNING",
}
_EPHEMERAL_CONTAINER_PREFIXES = ("tec-backend-run-",)


def _is_ephemeral_container(name: str) -> bool:
    return name.startswith(_EPHEMERAL_CONTAINER_PREFIXES)


async def _ingest_lifecycle(action: str, name: str, exit_code: Optional[int]) -> None:
    if action == "die":
        level = "WARNING" if exit_code is None or exit_code in _LIFECYCLE_CLEAN_EXITS else "ERROR"
    else:
        level = _LIFECYCLE_LEVELS.get(action)
    if not level:
        return
    service_id, service_name = CONTAINER_SERVICE_MAP.get(
        name, ("droplet-" + name, "DO " + name))
    msg = f"container {action}: {name} exit={exit_code}"
    if _mark_seen("docker-events", msg):
        return
    await _store_and_alert(ErrorEvent(
        service_id=service_id,
        service_name=service_name,
        level=level,
        message=msg,
        detail="docker lifecycle event",
        source="docker",
        timestamp=datetime.utcnow(),
    ))


async def _ingest_health_transition(container: str, new_health: str) -> bool:
    """Alert when a container's health status changes (unhealthy detection)."""
    prev = _health.get(container)
    _health[container] = new_health
    if new_health == prev or prev is None:
        return False
    service_id, service_name = CONTAINER_SERVICE_MAP.get(
        container, ("droplet-" + container, "DO " + container))
    if new_health == "unhealthy":
        level = "WARNING" if _is_ephemeral_container(container) else "ERROR"
        msg = f"container became UNHEALTHY: {container}"
    elif new_health == "exited":
        level, msg = "WARNING", f"container exited: {container}"
    elif prev == "unhealthy" and new_health == "healthy":
        level, msg = "WARNING", f"container recovered healthy: {container}"
    else:
        return False
    await _store_and_alert(ErrorEvent(
        service_id=service_id,
        service_name=service_name,
        level=level,
        message=msg,
        detail=f"health {prev} -> {new_health}",
        source="docker",
        timestamp=datetime.utcnow(),
    ))
    return True


async def _ingest_host_signal(level: str, key: str, message: str) -> None:
    """Host-level issues (OOM kill, disk, memory). Dedup + edge-triggered."""
    _host_flags[key] = True
    await _store_and_alert(ErrorEvent(
        service_id="droplet-host",
        service_name="Droplet Host",
        level=level,
        message=message[:500],
        detail="host-level signal",
        source="docker",
        timestamp=datetime.utcnow(),
    ))


async def _process_host(info: Dict) -> int:
    count = 0
    for raw in (info.get("oom_lines") or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        h = _sha(line)
        if h in _oom_seen_set:
            continue
        _oom_seen.append(h)
        _oom_seen_set.add(h)
        await _ingest_host_signal("CRITICAL", "oom", f"host OOM: {line[:300]}")
        count += 1

    disk = info.get("disk_pct")
    if disk is not None:
        if disk >= DISK_WARN_PCT and not _host_flags.get("disk_high"):
            await _ingest_host_signal(
                "WARNING", "disk_high", f"droplet disk usage {disk}%")
            count += 1
        elif disk < DISK_WARN_PCT:
            _host_flags["disk_high"] = False

    mem = info.get("mem_avail_pct")
    if mem is not None:
        if mem <= MEM_WARN_PCT and not _host_flags.get("mem_low"):
            await _ingest_host_signal(
                "WARNING", "mem_low", f"droplet memory available {mem}%")
            count += 1
        elif mem > MEM_WARN_PCT:
            _host_flags["mem_low"] = False
    return count


# Thresholds for the per-service checks
BACKUP_MAX_AGE_WARN_H = 26    # backup.sh runs every 6h -> 4 missed cycles
BACKUP_MAX_AGE_CRIT_H = 50
NGINX_5XX_WARN = 10           # 5xx responses in the poll window
NGINX_5XX_CRIT = 50
PG_CONN_WARN_PCT = 80


async def _service_check(level: str, key: str, container_svc: str,
                         service_name: str, message: str) -> bool:
    """Edge-triggered service check: fires once when entering bad state."""
    _host_flags[key] = True
    await _store_and_alert(ErrorEvent(
        service_id=container_svc,
        service_name=service_name,
        level=level,
        message=message[:500],
        detail=f"remote check: {key}",
        source="docker",
        timestamp=datetime.utcnow(),
    ))
    return True


async def _process_checks(checks: Dict) -> int:
    """Interpret remote read-only checks (pg, redis, backup, certbot, 5xx)."""
    count = 0

    # ── PostgreSQL ──────────────────────────────────────────────────────
    ready = checks.get("pg_ready", "")
    if ready and "accepting connections" not in ready:
        if not _host_flags.get("pg_down"):
            count += await _service_check(
                "ERROR", "pg_down", "tec-db", "ElenaTec DB Postgres",
                f"PostgreSQL not accepting connections: {ready}")
    else:
        _host_flags["pg_down"] = False
    locks = int(checks.get("pg_locks", "-1") or -1)
    if locks > 0 and not _host_flags.get("pg_locks"):
        count += await _service_check(
            "WARNING", "pg_locks", "tec-db", "ElenaTec DB Postgres",
            f"PostgreSQL: {locks} query(ies) blocked >60s on locks")
    elif locks == 0:
        _host_flags["pg_locks"] = False
    conn_pct = int(checks.get("pg_conn_pct", "-1") or -1)
    if conn_pct >= PG_CONN_WARN_PCT and not _host_flags.get("pg_conn"):
        count += await _service_check(
            "WARNING", "pg_conn", "tec-db", "ElenaTec DB Postgres",
            f"PostgreSQL connection usage at {conn_pct}% of max_connections")
    elif 0 <= conn_pct < PG_CONN_WARN_PCT:
        _host_flags["pg_conn"] = False

    # ── Redis ────────────────────────────────────────────────────────────
    if checks.get("redis_ping") == "DOWN":
        if not _host_flags.get("redis_down"):
            count += await _service_check(
                "ERROR", "redis_down", "tec-redis", "ElenaTec Redis",
                "Redis not responding to PING")
    else:
        _host_flags["redis_down"] = False

    # ── Backup staleness (tec-backup-1 runs /backup.sh every 6h) ─────────
    try:
        epoch = int(checks.get("backup_epoch", "0") or 0)
    except ValueError:
        epoch = 0
    if epoch:
        age_h = (datetime.utcnow().timestamp() - epoch) / 3600
        if age_h > BACKUP_MAX_AGE_CRIT_H and not _host_flags.get("backup_crit"):
            _host_flags["backup_crit"] = True
            count += await _service_check(
                "ERROR", "backup_crit", "tec-backup", "ElenaTec Backup",
                f"Backup not executed in {int(age_h)}h (expected every 6h)")
        elif age_h > BACKUP_MAX_AGE_WARN_H and not _host_flags.get("backup_warn"):
            _host_flags["backup_warn"] = True
            count += await _service_check(
                "WARNING", "backup_warn", "tec-backup", "ElenaTec Backup",
                f"Last backup {int(age_h)}h ago (expected every 6h)")
        elif age_h <= BACKUP_MAX_AGE_WARN_H:
            _host_flags["backup_warn"] = _host_flags["backup_crit"] = False
    elif not _host_flags.get("backup_none"):
        _host_flags["backup_none"] = True
        count += await _service_check(
            "ERROR", "backup_none", "tec-backup", "ElenaTec Backup",
            "No backup files found in /backups on tec-backup-1")

    # ── Certbot renewal failures ─────────────────────────────────────────
    certbot_bad = (
        checks.get("certbot_state") == "failed"
        or int(checks.get("certbot_errs", "0") or 0) > 0
    )
    if certbot_bad and not _host_flags.get("certbot_fail"):
        count += await _service_check(
            "WARNING", "certbot_fail", "droplet-host", "Droplet Host",
            f"certbot renewal problem: state={checks.get('certbot_state')} "
            f"errs48h={checks.get('certbot_errs')}")
    elif not certbot_bad:
        _host_flags["certbot_fail"] = False

    # ── nginx 5xx burst on the frontend proxy ────────────────────────────
    try:
        n5xx = int(checks.get("nginx5xx", "0") or 0)
    except ValueError:
        n5xx = 0
    if n5xx >= NGINX_5XX_CRIT and not _host_flags.get("n5xx_crit"):
        _host_flags["n5xx_crit"] = True
        count += await _service_check(
            "ERROR", "n5xx_crit", "tec-frontend", "ElenaTec Frontend (DO)",
            f"nginx returned {n5xx} 5xx responses in the poll window")
    elif n5xx >= NGINX_5XX_WARN and not _host_flags.get("n5xx_warn"):
        _host_flags["n5xx_warn"] = True
        count += await _service_check(
            "WARNING", "n5xx_warn", "tec-frontend", "ElenaTec Frontend (DO)",
            f"nginx returned {n5xx} 5xx responses in the poll window")
    elif n5xx < NGINX_5XX_WARN:
        _host_flags["n5xx_warn"] = _host_flags["n5xx_crit"] = False

    return count


# ── poll cycle ───────────────────────────────────────────────────────────────
async def _collect_once(since: str) -> int:
    data = await asyncio.wait_for(
        asyncio.to_thread(_fetch_all, SSH_HOST, SSH_USER, SSH_KEY, since),
        timeout=FETCH_TIMEOUT_S,
    )

    # Fallas de logs por contenedor: visibles en /api/health/sofia y en el
    # log del backend, sin tumbar el poll (el resto de fuentes se ingesta).
    log_failures = {name: err for name, err in (data.get("log_failures") or [])}
    if log_failures != (_status.get("log_failures") or {}):
        for name, err in log_failures.items():
            logger.warning(f"[DOCKER] logs no obtenidos de {name}: {err}")
        if not log_failures:
            logger.info("[DOCKER] todos los contenedores entregan logs de nuevo.")
        _status["log_failures"] = log_failures

    count = 0
    # 1) container health/status transitions
    for ln in (data.get("ps") or "").splitlines():
        if "|" not in ln:
            continue
        name, status = ln.split("|", 1)
        name = name.strip()
        status = status.strip().lower()
        if "unhealthy" in status:
            health = "unhealthy"
        elif "exited" in status:
            health = "exited"
        elif "healthy" in status:
            health = "healthy"
        else:
            health = "running"
        if await _ingest_health_transition(name, health):
            count += 1

    # 2) container log lines
    for container, text in (data.get("logs") or {}).items():
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            m = _TS_RE.match(line)
            ts, body = (m.group(1), m.group(2)) if m else ("", line)
            if await _ingest_log_line(container, ts, body):
                count += 1

    # 3) docker lifecycle events  "epoch|action|name|exit="
    for raw in (data.get("events") or "").splitlines():
        parts = raw.split("|")
        if len(parts) < 4:
            continue
        action, name, exit_raw = parts[1].strip(), parts[2].strip(), parts[3]
        exit_code = None
        m = re.search(r"exit=(\d+)", exit_raw)
        if m:
            exit_code = int(m.group(1))
        await _ingest_lifecycle(action, name, exit_code)
        count += 1

    # 4) host-level signals (OOM kills of ANY process, disk, memory)
    count += await _process_host(data.get("host") or {})

    # 5) per-service checks (pg, redis, backup staleness, certbot, 5xx)
    count += await _process_checks(data.get("checks") or {})
    return count


async def droplet_docker_loop() -> None:
    """Background task: poll the droplet's docker/host signals every POLL_SECONDS."""
    if not ENABLED:
        logger.info("[DOCKER] Droplet monitor disabled (SOFIA_DROPLET_MONITOR).")
        return
    try:
        import paramiko  # noqa: F401
    except ImportError:
        logger.error("[DOCKER] paramiko not installed — droplet monitor inactive.")
        _status["last_error"] = "paramiko not installed"
        return

    _load_state()
    logger.info(
        f"[DOCKER] Droplet monitor started (host={SSH_HOST} user={SSH_USER} "
        f"poll={POLL_SECONDS}s).")

    first = True
    while True:
        # Adaptive window: cover everything since the last SUCCESSFUL poll so a
        # backoff gap after an SSH failure never drops error lines. The window
        # is a duration, so droplet/host clock skew does not matter.
        if first:
            since = f"{BACKFILL_MINUTES}m"
        elif _last_success_iso:
            try:
                gap = (datetime.utcnow()
                       - datetime.fromisoformat(_last_success_iso)).total_seconds()
                since = f"{min(max(POLL_SECONDS * 2, int(gap) + 90), 7200)}s"
            except (TypeError, ValueError):
                since = f"{max(POLL_SECONDS * 2, 180)}s"
        else:
            since = f"{max(POLL_SECONDS * 2, 180)}s"
        first = False
        try:
            n = await _collect_once(since)
            _last_success_iso = datetime.utcnow().isoformat()
            _status["last_poll"] = _last_success_iso
            _status["last_error"] = None
            _status["last_ingest_count"] = n
            _status["consecutive_failures"] = 0
            if n:
                logger.info(f"[DOCKER] Ingested {n} docker event(s).")
            _save_state()
            await asyncio.sleep(POLL_SECONDS)
        except Exception as exc:
            fails = int(_status.get("consecutive_failures") or 0) + 1
            _status["consecutive_failures"] = fails
            _status["last_error"] = str(exc)[:300]
            logger.warning(f"[DOCKER] Poll failed ({fails}x): {exc}")
            # Record an unreachable-droplet issue (deduped by fingerprint) but
            # do NOT alert — tec/sofiatunnel_e2e health checks already cover
            # a real outage with CRITICAL alerts.
            try:
                await db_service.upsert_event(ErrorEvent(
                    service_id="droplet-ssh",
                    service_name="Droplet SSH",
                    level="WARNING",
                    message=f"droplet docker poll failed: {str(exc)[:200]}",
                    source="docker",
                    timestamp=datetime.utcnow(),
                ))
            except Exception:
                pass
            # modest backoff so we don't hammer a struggling droplet
            await asyncio.sleep(min(POLL_SECONDS * fails, 600))
