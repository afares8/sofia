"""
Passive log monitoring - tail log files and detect ERROR/WARNING lines.
Fires alerts and stores events in DB when errors are found.
"""
import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict
from app.models.event import ErrorEvent
from app.services import db_service, whatsapp_service
from app.services.config_service import load_config

logger = logging.getLogger("sofia.logs")

# Track file positions between tails
_file_positions: Dict[str, int] = {}

# Regex to detect log levels
_LEVEL_RE = re.compile(r"\b(ERROR|CRITICAL|WARNING|WARN)\b", re.IGNORECASE)


def _classify_level(line: str) -> str | None:
    m = _LEVEL_RE.search(line)
    if not m:
        return None
    lvl = m.group(1).upper()
    return "WARNING" if lvl in ("WARNING", "WARN") else lvl


async def tail_log(service_id: str, service_name: str, log_path: str, tail_lines: int = 200):
    """Read new lines from log file since last check. Returns new error lines found."""
    path = Path(log_path)
    if not path.exists():
        return []

    current_size = path.stat().st_size
    last_pos = _file_positions.get(log_path, max(0, current_size - 8192))

    if current_size < last_pos:
        # File was rotated
        last_pos = 0

    if current_size == last_pos:
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(last_pos)
            new_lines = f.readlines()
            _file_positions[log_path] = f.tell()
    except Exception as exc:
        logger.warning(f"[LOGS] Cannot read {log_path}: {exc}")
        return []

    events = []
    cfg = load_config()
    for line in new_lines:
        line = line.strip()
        if not line:
            continue
        level = _classify_level(line)
        if not level:
            continue

        event = ErrorEvent(
            service_id=service_id,
            service_name=service_name,
            level=level,
            message=line[:500],
            source="passive",
            timestamp=datetime.utcnow(),
        )
        event_id = await db_service.insert_event(event)
        events.append(event)

        # Alert on ERROR or CRITICAL
        if level in ("ERROR", "CRITICAL"):
            sent = await whatsapp_service.send_alert(
                cfg.alerts, service_name, service_id,
                level, line[:200]
            )
            if sent:
                await db_service.mark_notified(event_id)

    return events


async def read_log_tail(log_path: str, lines: int = 100) -> list[str]:
    """Return the last N lines of a log file (for UI display)."""
    path = Path(log_path)
    if not path.exists():
        return [f"[Log file not found: {log_path}]"]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return [l.rstrip() for l in all_lines[-lines:]]
    except Exception as exc:
        return [f"[Error reading log: {exc}]"]


async def log_poll_loop():
    """Background task: tail log files for all services with log_path configured."""
    logger.info("[LOGS] Log poll loop started.")
    while True:
        cfg = load_config()
        for svc in cfg.services:
            if svc.enabled and svc.log_path:
                try:
                    await tail_log(svc.id, svc.name, svc.log_path, cfg.log_tail_lines)
                except Exception as exc:
                    logger.error(f"[LOGS] Error tailing {svc.id}: {exc}")
        await asyncio.sleep(cfg.poll_interval_seconds)
