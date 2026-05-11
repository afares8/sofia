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
"""
import asyncio
import logging
import traceback
from typing import Optional

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("sofia_sdk")

SOFIA_URL = "http://localhost:5180/api/ingest/event"
_sofia_url: str = SOFIA_URL


def configure(sofia_url: str = SOFIA_URL):
    global _sofia_url
    _sofia_url = sofia_url


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
        payload = {
            "service_id": service_id,
            "service_name": service_name,
            "level": level.upper(),
            "message": message[:1000],
            "detail": (detail or "")[:3000],
            "traceback": (tb or "")[:8000],
            "url": url,
            "user_info": user_info,
        }
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
        payload = {
            "service_id": service_id,
            "service_name": service_name,
            "level": level.upper(),
            "message": message[:1000],
            "detail": (detail or "")[:3000],
            "traceback": (tb or "")[:8000],
            "url": url,
            "user_info": user_info,
        }
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
    )
    """

    def __init__(self, app, service_id: str, service_name: str,
                 sofia_url: str = SOFIA_URL):
        super().__init__(app)
        self.service_id = service_id
        self.service_name = service_name
        configure(sofia_url)

    def _get_user(self, request: Request) -> Optional[str]:
        """Try to extract user info from JWT or headers."""
        try:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                import base64, json as _json
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
            msg = self.format(record)
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
