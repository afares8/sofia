"""
Sofia SDK - Drop-in Sentry-like error reporter for FastAPI apps.

Usage (one line in your main.py):
    from sofia_sdk import SofiaMiddleware
    app.add_middleware(SofiaMiddleware, service_id="mayor", service_name="Mayor")

Manual reporting:
    from sofia_sdk import report_error
    await report_error("mayor", "Mayor", "ERROR", "Something broke", detail="...")

Logging handler (captures all logger.error / logger.critical automatically):
    from sofia_sdk import SofiaLogHandler
    logging.getLogger().addHandler(SofiaLogHandler("mayor", "Mayor"))

Breadcrumbs:
    The SDK keeps a circular buffer of the last 20 events (HTTP requests, log
    records, manual breadcrumbs) per process. When an error is reported, the
    breadcrumbs are attached automatically so you can see what happened just
    before the error.

    from sofia_sdk import add_breadcrumb
    add_breadcrumb(category="auth", message="user logged in", data={"user_id": 42})
"""
import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("sofia_sdk")

SOFIA_URL = "http://localhost:5180/api/ingest/event"
_sofia_url: str = SOFIA_URL
_environment: Optional[str] = None
_release: Optional[str] = None
_default_tags: Dict[str, Any] = {}

# Breadcrumb circular buffer (last 20 events).
BREADCRUMBS_MAX = 20
_breadcrumbs: Deque[dict] = deque(maxlen=BREADCRUMBS_MAX)
_breadcrumbs_lock = threading.Lock()

# Disk spool: when Sofia is unreachable, events are appended to a local JSONL
# file and replayed by a daemon flusher thread once Sofia comes back. This is
# what makes remote monitoring robust — network partitions lose nothing.
SPOOL_MAX_BYTES = 512 * 1024
SPOOL_TRIM_ENTRIES = 500
FLUSH_INTERVAL_SECONDS = 15
_spool_dir: Optional[str] = None
_spool_lock = threading.Lock()
_flushers: set = set()
_flushers_lock = threading.Lock()

SLOW_REQUEST_MS_DEFAULT = 5000.0


def configure(
    sofia_url: str = SOFIA_URL,
    environment: Optional[str] = None,
    release: Optional[str] = None,
    tags: Optional[Dict[str, Any]] = None,
    spool_dir: Optional[str] = None,
):
    global _sofia_url, _environment, _release, _default_tags, _spool_dir
    _sofia_url = sofia_url
    if environment is not None:
        _environment = environment
    if release is not None:
        _release = release
    if tags is not None:
        _default_tags = dict(tags)
    if spool_dir is not None:
        _spool_dir = spool_dir


def _spool_file(service_id: str) -> Path:
    directory = _spool_dir or os.environ.get("SOFIA_SPOOL_DIR") or tempfile.gettempdir()
    return Path(directory) / f"sofia-spool-{service_id}.jsonl"


def _spool_write(line: str, path: Path) -> None:
    """Append one serialized payload line, trimming the head when over capacity."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _spool_lock:
            if path.exists() and path.stat().st_size + len(line) > SPOOL_MAX_BYTES:
                old = path.read_text(encoding="utf-8").splitlines(keepends=True)
                path.write_text("".join(old[-SPOOL_TRIM_ENTRIES:]), encoding="utf-8")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass


def _spool_append(payload: dict) -> None:
    _spool_write(json.dumps(payload, separators=(",", ":")) + "\n", _spool_file(payload["service_id"]))


def _spool_drain(service_id: str) -> None:
    """Replay spooled events; failures are re-appended so nothing is lost.

    The spool is renamed aside before posting (unique per pid) so appends
    from other processes during the drain land in a fresh file — with
    multi-worker servers (e.g. uvicorn --workers 4) nothing is dropped.
    """
    path = _spool_file(service_id)
    drain_path = path.with_name(f"{path.name}.{os.getpid()}.drain")
    with _spool_lock:
        try:
            # Re-merge drain files abandoned by dead processes.
            for stale in path.parent.glob(f"{path.name}.*.drain"):
                try:
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write(stale.read_text(encoding="utf-8"))
                    stale.unlink()
                except Exception:
                    pass
            if not path.exists():
                return
            drain_path.unlink(missing_ok=True)
            path.rename(drain_path)
            lines = drain_path.read_text(encoding="utf-8").splitlines()
            drain_path.unlink(missing_ok=True)
        except Exception:
            return
    if not lines:
        return
    failed: List[str] = []
    try:
        with httpx.Client(timeout=3) as client:
            for line in lines:
                try:
                    resp = client.post(_sofia_url, content=line, headers={"Content-Type": "application/json"})
                    if resp.status_code >= 300:
                        failed.append(line)
                except Exception:
                    failed.append(line)
    except Exception:
        failed = list(lines)
    for line in failed:
        _spool_write(line + "\n", path)


def _flusher_loop(service_id: str) -> None:
    while True:
        try:
            _spool_drain(service_id)
        except Exception:
            pass
        time.sleep(FLUSH_INTERVAL_SECONDS)


def _ensure_flusher(service_id: str) -> None:
    """Start the per-service drain thread once (also replays stale spools after restart)."""
    with _flushers_lock:
        if service_id in _flushers:
            return
        _flushers.add(service_id)
    threading.Thread(target=_flusher_loop, args=(service_id,), daemon=True, name=f"sofia-flush-{service_id}").start()


def add_breadcrumb(
    category: str = "default",
    message: str = "",
    level: str = "info",
    data: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a breadcrumb to the in-memory buffer. Thread-safe."""
    crumb = {
        "timestamp": time.time(),
        "category": category,
        "message": str(message)[:300],
        "level": level,
        "data": data or {},
    }
    with _breadcrumbs_lock:
        _breadcrumbs.append(crumb)


def get_breadcrumbs() -> List[dict]:
    with _breadcrumbs_lock:
        return list(_breadcrumbs)


def _build_payload(
    service_id: str,
    service_name: str,
    level: str,
    message: str,
    detail: Optional[str],
    tb: Optional[str],
    url: Optional[str],
    user_info: Optional[str],
) -> dict:
    payload = {
        "service_id": service_id,
        "service_name": service_name,
        "level": level.upper(),
        "message": message[:1000],
        "detail": (detail or "")[:3000],
        "traceback": (tb or "")[:8000],
        "url": url,
        "user_info": user_info,
        "breadcrumbs": get_breadcrumbs(),
    }
    if _environment:
        payload["environment"] = _environment
    if _release:
        payload["release"] = _release
    if _default_tags:
        payload["tags"] = _default_tags
    return payload


async def report_error(
    service_id: str,
    service_name: str,
    level: str,
    message: str,
    detail: Optional[str] = None,
    tb: Optional[str] = None,
    url: Optional[str] = None,
    user_info: Optional[str] = None,
) -> bool:
    """Send an error event to Sofia Monitor. Never raises — safe to fire-and-forget."""
    payload = _build_payload(service_id, service_name, level, message,
                             detail, tb, url, user_info)
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.post(_sofia_url, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"Sofia returned {resp.status_code}")
        return True
    except Exception as exc:
        logger.debug(f"[sofia_sdk] Could not report to Sofia (spooled): {exc}")
        _spool_append(payload)
        _ensure_flusher(service_id)
        return False


def report_error_sync(
    service_id: str,
    service_name: str,
    level: str,
    message: str,
    detail: Optional[str] = None,
    tb: Optional[str] = None,
    url: Optional[str] = None,
    user_info: Optional[str] = None,
) -> bool:
    """Sync version — use from logging handlers or non-async contexts."""
    payload = _build_payload(service_id, service_name, level, message,
                             detail, tb, url, user_info)
    try:
        with httpx.Client(timeout=3) as client:
            resp = client.post(_sofia_url, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"Sofia returned {resp.status_code}")
        return True
    except Exception as exc:
        logger.debug(f"[sofia_sdk] Could not report to Sofia (spooled): {exc}")
        _spool_append(payload)
        _ensure_flusher(service_id)
        return False


class SofiaMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware — catches ALL unhandled exceptions and 500 responses
    and reports them to Sofia with full context: URL, method, user, traceback.

    app.add_middleware(
        SofiaMiddleware,
        service_id="mayor",
        service_name="Mayor",
        sofia_url="http://localhost:5180",  # optional
        environment="prod",                 # optional
        release="1.2.0",                    # optional
    )
    """

    def __init__(
        self,
        app,
        service_id: str,
        service_name: str,
        sofia_url: str = SOFIA_URL,
        environment: Optional[str] = None,
        release: Optional[str] = None,
        tags: Optional[Dict[str, Any]] = None,
        slow_ms: Optional[float] = None,
    ):
        super().__init__(app)
        self.service_id = service_id
        self.service_name = service_name
        self.slow_ms = slow_ms if slow_ms is not None else float(
            os.environ.get("SOFIA_SLOW_MS", str(SLOW_REQUEST_MS_DEFAULT)))
        configure(sofia_url, environment=environment, release=release, tags=tags)
        _ensure_flusher(service_id)

    def _get_user(self, request: Request) -> Optional[str]:
        """Try to extract user info from JWT or headers."""
        try:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                import base64
                import json as _json
                token = auth.split(".")[1]
                token += "=" * (-len(token) % 4)
                payload = _json.loads(base64.b64decode(token))
                return payload.get("sub") or payload.get("email") or payload.get("username")
        except Exception:
            pass
        return request.headers.get("x-user-id") or request.headers.get("x-forwarded-for")

    async def dispatch(self, request: Request, call_next):
        url = str(request.url)
        method = request.method
        user = self._get_user(request)
        t0 = time.monotonic()

        try:
            response = await call_next(request)
            duration_ms = (time.monotonic() - t0) * 1000
            # Drop a breadcrumb for every request
            add_breadcrumb(
                category="http", level="info",
                message=f"{method} {request.url.path} {response.status_code}",
                data={"method": method, "url": str(request.url.path),
                      "status_code": response.status_code,
                      "duration_ms": round(duration_ms, 1)},
            )
            if response.status_code >= 500:
                asyncio.create_task(report_error(
                    self.service_id, self.service_name, "ERROR",
                    f"HTTP {response.status_code} — {method} {request.url.path}",
                    detail=f"Method: {method}\nURL: {url}",
                    url=url, user_info=user,
                ))
            elif duration_ms > self.slow_ms:
                # Message omits the duration so repeated slow hits on the same
                # endpoint group into one issue (fingerprint = message hash).
                asyncio.create_task(report_error(
                    self.service_id, self.service_name, "WARNING",
                    f"Slow request — {method} {request.url.path}",
                    detail=f"Method: {method}\nURL: {url}\nDuration: {duration_ms:.0f}ms",
                    url=url, user_info=user,
                ))
            return response
        except Exception as exc:
            tb = traceback.format_exc()
            add_breadcrumb(
                category="http", level="error",
                message=f"{method} {request.url.path} CRASH",
                data={"method": method, "url": str(request.url.path)},
            )
            asyncio.create_task(report_error(
                self.service_id, self.service_name, "CRITICAL",
                f"{type(exc).__name__}: {exc}",
                detail=f"Method: {method}\nURL: {url}",
                tb=tb, url=url, user_info=user,
            ))
            raise


class SofiaLogHandler(logging.Handler):
    """
    Python logging handler — attach to any logger to automatically send
    ERROR and CRITICAL log records to Sofia, even without a log file.

    # Capture all app errors globally:
    logging.getLogger().addHandler(SofiaLogHandler("mayor", "Mayor"))

    # Or only a specific logger:
    logging.getLogger("app").addHandler(SofiaLogHandler("mayor", "Mayor"))
    """

    def __init__(self, service_id: str, service_name: str,
                 sofia_url: str = SOFIA_URL,
                 min_level: int = logging.ERROR):
        super().__init__()
        self.service_id = service_id
        self.service_name = service_name
        self.min_level = min_level
        configure(sofia_url)
        _ensure_flusher(service_id)

    def emit(self, record: logging.LogRecord):
        if record.levelno < self.min_level:
            return
        # Skip Sofia's own logs to avoid infinite loops
        if record.name.startswith("sofia"):
            return
        try:
            tb = None
            if record.exc_info:
                tb = "".join(traceback.format_exception(*record.exc_info))
            level = "CRITICAL" if record.levelno >= logging.CRITICAL else "ERROR"
            report_error_sync(
                self.service_id, self.service_name, level,
                record.getMessage()[:500],
                detail=f"Logger: {record.name}\nFile: {record.pathname}:{record.lineno}",
                tb=tb,
            )
        except Exception:
            pass
