"""
Passive log monitoring - tail log files and detect ERROR/WARNING lines.
Fires alerts and stores events in DB when errors are found.
"""
import asyncio
import logging
import os
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from app.models.event import ErrorEvent
from app.services import db_service, whatsapp_service
from app.services.config_service import load_config
from app.services.event_classification import normalize_reported_level

logger = logging.getLogger("sofia.logs")

# Track file positions between tails
_file_positions: Dict[str, int] = {}
_file_identities: Dict[str, tuple] = {}
_path_locks: Dict[str, asyncio.Lock] = {}
_resolve_errors: Dict[str, str] = {}
_overlong_pending: set = set()

_MAX_LINE_BYTES = 65536
_STARTUP_TAIL_BYTES = 8192

_monitor_status: Dict = {"last_poll": None, "sources": {}}

_DEFAULT_LOG_PATHS = {
    "mayor": Path(__file__).resolve().parents[4] / "mayor" / "backend" / "logs" / "app.log",
    "packing": Path(__file__).resolve().parents[4] / "Packing" / "backend" / "logs" / "app.log",
}
_NSSM_SERVICES = {"mayor": "MayorBackend", "packing": "PackingBackend"}

# Regex to detect log levels
_LEVEL_RE = re.compile(r"\b(ERROR|CRITICAL|WARNING|WARN)\b", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HEAD_RE = re.compile(
    r"^(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG|TRACE|NOTICE)\b", re.IGNORECASE
)
_BRACKET_HEAD_RE = re.compile(
    r"^\[(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG|TRACE|NOTICE)\]\s*(.*)$",
    re.IGNORECASE | re.S,
)
_TS_LEVEL_RE = re.compile(
    r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s*"
    r"\[?\s*(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG|TRACE|NOTICE)\s*\]?\s*"
    r"(?:[^\s:]{1,80}:\s*)?(.*)$",
    re.IGNORECASE | re.S,
)
_PY_LOGGING_RE = re.compile(
    r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+-\s+\S+\s+-\s+"
    r"(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG|TRACE|NOTICE)\s+-\s+(.*)$",
    re.IGNORECASE | re.S,
)
_LOGGER_PREFIX_RE = re.compile(r"^[^\s:]{1,80}:")


def _norm_path(p) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower()


def _nssm_stderr_path(service_id: str) -> Optional[Path]:
    svc_name = _NSSM_SERVICES.get(service_id)
    if not svc_name or os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SYSTEM\CurrentControlSet\Services\{svc_name}\Parameters",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppStderr")
        return Path(str(value)) if value else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        _resolve_errors[service_id] = f"nssm registry read failed: {exc}"
        return None


def _resolve_log_path(service_id: str, configured_path: str) -> Optional[Path]:
    """Resolve the effective log path for a service (configured file or NSSM stderr)."""
    _resolve_errors.pop(service_id, None)
    cfg = Path(configured_path) if configured_path else None
    try:
        if cfg is not None and cfg.is_file():
            return cfg
    except Exception as exc:
        _resolve_errors[service_id] = f"configured path stat failed: {exc}"
    default = _DEFAULT_LOG_PATHS.get(service_id)
    candidate = cfg
    if (
        default is not None
        and cfg is not None
        and _norm_path(cfg) == _norm_path(default)
    ):
        fallback = _nssm_stderr_path(service_id)
        if fallback is not None:
            try:
                if fallback.is_file():
                    return fallback
                _resolve_errors[service_id] = f"nssm stderr not a file: {fallback}"
            except Exception as exc:
                _resolve_errors[service_id] = f"nssm stderr stat failed: {exc}"
        elif service_id not in _resolve_errors:
            _resolve_errors[service_id] = "default path missing; no nssm fallback"
        candidate = cfg
    return candidate


def _split_line(line: str):
    """Return (reported_level|None, canonical_message) for a raw log line."""
    m = _PY_LOGGING_RE.match(line) or _TS_LEVEL_RE.match(line)
    if m:
        return m.group(1).upper(), (m.group(2) or "").strip()
    m = _HEAD_RE.match(line)
    if m:
        level = m.group(1).upper()
        rest = line[m.end():].strip()
        if rest.startswith(":"):
            rest = rest[1:].strip()
        if _LOGGER_PREFIX_RE.match(rest):
            rest = rest.split(":", 1)[1].strip()
        return level, rest
    m = _BRACKET_HEAD_RE.match(line)
    if m:
        return m.group(1).upper(), (m.group(2) or "").strip()
    return None, line


def _classify_level(line: str) -> Optional[str]:
    reported, message = _split_line(line)
    if reported is not None:
        if reported not in ("ERROR", "CRITICAL", "WARNING", "WARN"):
            return None
        level = "WARNING" if reported == "WARN" else reported
        return normalize_reported_level(level, message)
    m = _LEVEL_RE.search(line)
    if not m:
        return None
    level = m.group(1).upper()
    if level == "WARN":
        level = "WARNING"
    return normalize_reported_level(level, line)


def get_monitor_status() -> dict:
    """Return a copy of the log monitor status for health reporting."""
    return {
        "last_poll": _monitor_status.get("last_poll"),
        "sources": {k: dict(v) for k, v in _monitor_status["sources"].items()},
    }


def _transition(src: dict, state: str, error: Optional[str] = None) -> None:
    prev = src.get("state")
    if prev != state or src.get("error") != error:
        if prev is not None and prev != state:
            logger.warning(
                f"[LOGS] {src.get('service_id')}: {prev} -> {state} ({error})"
            )
        src["state"] = state
        src["error"] = error


async def tail_log(service_id: str, service_name: str, log_path: str, tail_lines: int = 200):
    """Read new lines from log file since last check. Returns new error lines found."""
    sources = _monitor_status["sources"]
    src = sources.setdefault(service_id, {"service_id": service_id})
    src["configured_path"] = log_path

    path = _resolve_log_path(service_id, log_path)
    src["effective_path"] = str(path) if path is not None else None
    try:
        path_ok = path is not None and path.is_file()
    except Exception as exc:
        _transition(src, "error", str(exc)[:300])
        src["pending_bytes"] = 0
        return []
    if not path_ok:
        _transition(src, "missing", _resolve_errors.get(service_id) or "log file not found")
        src["pending_bytes"] = 0
        src["drain_backlog"] = False
        return []

    if tail_lines <= 0:
        _transition(src, "error", f"invalid tail_lines={tail_lines}")
        src["pending_bytes"] = 0
        src["drain_backlog"] = False
        return []

    lock = _path_locks.setdefault(str(path), asyncio.Lock())
    events = []
    cfg = load_config()
    async with lock:
        try:
            st = path.stat()
        except Exception as exc:
            _transition(src, "error", str(exc)[:300])
            src["drain_backlog"] = False
            return []
        size = st.st_size
        ident = (st.st_dev, st.st_ino)
        key = str(path)
        pos = _file_positions.get(key)
        startup_seek = False
        if _file_identities.get(key) is not None and _file_identities[key] != ident:
            pos = 0
            _overlong_pending.discard(key)
        elif pos is None:
            pos = max(0, size - _STARTUP_TAIL_BYTES)
            startup_seek = pos > 0
        if size < pos:
            # File was rotated
            pos = 0
            _overlong_pending.discard(key)
        _file_identities[key] = ident
        _file_positions[key] = pos
        if size == pos:
            _transition(src, "ready", None)
            src["pending_bytes"] = 0
            src["drain_backlog"] = False
            return []

        lines = []
        truncated = False
        staged_cursor = pos
        overlong = key in _overlong_pending
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                if startup_seek:
                    f.seek(pos - 1)
                    if f.read(1) != b"\n":
                        f.readline(_MAX_LINE_BYTES + 1)
                    _file_positions[key] = f.tell()
                    staged_cursor = _file_positions[key]
                while len(lines) < tail_lines:
                    raw = f.readline(_MAX_LINE_BYTES + 1)
                    if not raw:
                        break
                    if overlong:
                        staged_cursor = f.tell()
                        if raw.endswith(b"\n"):
                            overlong = False
                            continue
                        break
                    if raw.endswith(b"\n"):
                        staged_cursor = f.tell()
                        lines.append((raw, False, f.tell()))
                        continue
                    if len(raw) <= _MAX_LINE_BYTES:
                        break
                    truncated = True
                    head = raw[:_MAX_LINE_BYTES]
                    hit_eof = True
                    while True:
                        chunk = f.readline(_MAX_LINE_BYTES + 1)
                        if not chunk:
                            break
                        if chunk.endswith(b"\n"):
                            hit_eof = False
                            break
                    staged_cursor = f.tell()
                    if hit_eof:
                        overlong = True
                        break
                    lines.append((head, True, f.tell()))
        except Exception as exc:
            _transition(src, "error", str(exc)[:300])
            src["drain_backlog"] = False
            return events

        for raw, was_truncated, pos_after in lines:
            try:
                # Quitar codigos ANSI (p. ej. WPPConnect escribe \x1b[31merror\x1b[39m)
                # para que el nivel se clasifique y el mensaje quede legible.
                text = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).strip()
            except Exception:
                text = ""
            if not text:
                _file_positions[key] = pos_after
                continue
            level = _classify_level(text)
            if not level:
                _file_positions[key] = pos_after
                continue
            _, message = _split_line(text)
            if was_truncated:
                message = (message + " [truncated]")[:600]
            event = ErrorEvent(
                service_id=service_id,
                service_name=service_name,
                level=level,
                message=(message or text)[:500],
                detail=text[:1000],
                source="passive",
                timestamp=datetime.utcnow(),
            )
            try:
                issue_id, is_new, is_regression = await db_service.upsert_event(event)
            except Exception as exc:
                _transition(src, "error", f"db persist failed: {exc}"[:300])
                src["drain_backlog"] = False
                return events
            _file_positions[key] = pos_after
            if issue_id == 0:
                continue
            events.append(event)

            # Alert on ERROR or CRITICAL
            if level in ("ERROR", "CRITICAL") and (is_new or is_regression):
                prefix = "🔄 REGRESIÓN: " if is_regression else ""
                try:
                    sent = await whatsapp_service.send_alert(
                        cfg.alerts, service_name, service_id,
                        level, prefix + (message or text)[:200]
                    )
                    if sent:
                        await db_service.mark_notified(issue_id)
                except Exception as exc:
                    logger.warning(f"[LOGS] alert failed for {service_id}: {exc}")

        if overlong:
            _overlong_pending.add(key)
        else:
            _overlong_pending.discard(key)
        _file_positions[key] = staged_cursor
        pending = max(0, size - staged_cursor)
        src["pending_bytes"] = pending
        src["drain_backlog"] = len(lines) == tail_lines and pending > 0
        if truncated:
            _transition(src, "error", "oversized line truncated")
        else:
            _transition(src, "ready", None)

    return events


async def read_log_tail(log_path: str, lines: int = 100) -> list[str]:
    """Return the last N lines of a log file (for UI display)."""
    path = Path(log_path)
    if not path.exists():
        return [f"[Log file not found: {log_path}]"]
    try:
        tail: deque = deque(maxlen=lines)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                tail.append(line)
        return [l.rstrip() for l in tail]
    except Exception as exc:
        return [f"[Error reading log: {exc}]"]


async def log_poll_loop():
    """Background task: tail log files for all services with log_path configured."""
    logger.info("[LOGS] Log poll loop started.")
    while True:
        cfg = load_config()
        active = set()
        for svc in cfg.services:
            if svc.enabled and svc.log_path:
                active.add(svc.id)
                try:
                    await tail_log(svc.id, svc.name, svc.log_path, cfg.log_tail_lines)
                except Exception as exc:
                    logger.error(f"[LOGS] Error tailing {svc.id}: {exc}")
        for sid in list(_monitor_status["sources"]):
            if sid not in active:
                del _monitor_status["sources"][sid]
        _monitor_status["last_poll"] = datetime.utcnow().isoformat()
        backlog = any(
            s.get("state") == "ready" and s.get("drain_backlog")
            for s in _monitor_status["sources"].values()
        )
        await asyncio.sleep(0 if backlog else cfg.poll_interval_seconds)
