"""
Sofia SDK - Drop-in error reporter for FastAPI apps.

Usage (add to your main.py):
    from sofia_sdk import SofiaMiddleware
    app.add_middleware(SofiaMiddleware, service_id="mayor", service_name="Mayor")

Or manual reporting:
    from sofia_sdk import report_error
    await report_error("mayor", "Mayor", "ERROR", "Something broke", detail="...")
"""
import asyncio
import logging
import traceback
from typing import Optional

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("sofia_sdk")

SOFIA_URL = "http://localhost:9000/api/ingest/event"
_sofia_url: str = SOFIA_URL


def configure(sofia_url: str = SOFIA_URL):
    """Override the Sofia Monitor URL if needed."""
    global _sofia_url
    _sofia_url = sofia_url


async def report_error(
    service_id: str,
    service_name: str,
    level: str,
    message: str,
    detail: Optional[str] = None,
    tb: Optional[str] = None,
) -> bool:
    """Send an error event to Sofia Monitor (fire-and-forget, never raises)."""
    try:
        payload = {
            "service_id": service_id,
            "service_name": service_name,
            "level": level.upper(),
            "message": message[:1000],
            "detail": (detail or "")[:2000],
            "traceback": (tb or "")[:5000],
        }
        async with httpx.AsyncClient(timeout=3) as client:
            await client.post(_sofia_url, json=payload)
        return True
    except Exception as exc:
        logger.debug(f"[sofia_sdk] Could not report to Sofia: {exc}")
        return False


class SofiaMiddleware(BaseHTTPMiddleware):
    """
    FastAPI/Starlette middleware that catches unhandled 500 errors
    and reports them to Sofia Monitor automatically.

    Add to your app AFTER CORSMiddleware:
        app.add_middleware(
            SofiaMiddleware,
            service_id="mayor",
            service_name="Mayor",
            sofia_url="http://localhost:9000",   # optional
        )
    """

    def __init__(self, app, service_id: str, service_name: str, sofia_url: str = SOFIA_URL):
        super().__init__(app)
        self.service_id = service_id
        self.service_name = service_name
        configure(sofia_url)

    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
            # Report 500s even if they're handled by exception handlers
            if response.status_code >= 500:
                asyncio.create_task(
                    report_error(
                        self.service_id,
                        self.service_name,
                        "ERROR",
                        f"HTTP {response.status_code} on {request.method} {request.url.path}",
                    )
                )
            return response
        except Exception as exc:
            tb = traceback.format_exc()
            asyncio.create_task(
                report_error(
                    self.service_id,
                    self.service_name,
                    "CRITICAL",
                    f"Unhandled exception: {type(exc).__name__}: {exc}",
                    detail=str(request.url),
                    tb=tb,
                )
            )
            raise
