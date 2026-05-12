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
import logging
import threading
import time
import traceback
from collections import deque
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


def configure(
    sofia_url: str = SOFIA_URL,
    environment: Optional[str] = None,
    release: Optional[str] = None,
    tags: Optional[Dict[str, Any]] = None,
):
    global _sofia_url, _environment, _release, _default_tags
    _sofia_url = sofia_url
    if environment is not None:
        _environment = environment
    if release is not None:
        _release = release
    if tags is not None:
        _default_tags = dict(tags)


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
    try:
        payload = _build_payload(service_id, service_name, level, message,
                                 detail, tb, url, user_info)
        async with httpx.AsyncClient(timeout=3) as client:
            await client.post(_sofia_url, json=payload)
        return True
    except Exception as exc:
        logger.debug(f"[sofia_sdk] Could not report to Sofia: {exc}")
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
    try:
        payload = _build_payload(service_id, service_name, level, message,
                                 detail, tb, url, user_info)
        with httpx.Client(timeout=3) as client:
            client.post(_sofia_url, json=payload)
        return True
    except Exception as exc:
        logger.debug(f"[sofia_sdk] Could not report to Sofia: {exc}")
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
    ):
        super().__init__(app)
        self.service_id = service_id
        self.service_name = service_name
        configure(sofia_url, environment=environment, release=release, tags=tags)

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

        try:
            response = await call_next(request)
            # Drop a breadcrumb for every request
            add_breadcrumb(
                category="http", level="info",
                message=f"{method} {request.url.path} {response.status_code}",
                data={"method": method, "url": str(request.url.path),
                      "status_code": response.status_code},
            )
            if response.status_code >= 500:
                asyncio.create_task(report_error(
                    self.service_id, self.service_name, "ERROR",
                    f"HTTP {response.status_code} — {method} {request.url.path}",
                    detail=f"Method: {method}\nURL: {url}",
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
