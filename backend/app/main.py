"""
Sofia Monitor - Main FastAPI application.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.routers import health, events, ingest, logs, config, webhook, restore, nightly, autonomy
from app.services.alert_queue import alert_queue_loop
from app.services.analytics_service import spike_detection_loop
from app.services.config_service import load_config
from app.services.db_service import init_db, purge_old_events
from app.services.health_service import poll_loop
from app.services.log_service import log_poll_loop
from app.services.nightly_review_service import nightly_loop
from app.services.rules_engine import rules_loop
from app.services.autonomy_service import autofix_loop, job_watchdog_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sofia")


# Paths exempt from API-key auth (SDKs and WPPConnect can't send a header).
AUTH_EXEMPT_PREFIXES = (
    "/api/ping",
    "/api/ingest/event",
    "/api/webhook/wppconnect",
)


async def _register_wppconnect_webhook():
    """Register Sofia's webhook URL in WPPConnect so it receives incoming messages."""
    import httpx
    await asyncio.sleep(5)  # Wait for WPPConnect to be ready
    cfg = load_config()
    sofia_external_url = os.getenv("SOFIA_EXTERNAL_URL", "http://localhost:5180").rstrip("/")
    webhook_url = f"{sofia_external_url}/api/webhook/wppconnect"
    url = f"{cfg.alerts.wppconnect_url}/api/{cfg.alerts.wppconnect_session}/webhook"
    headers = {"Authorization": f"Bearer {cfg.alerts.wppconnect_token}"}
    payload = {"webhook": webhook_url, "events": ["onMessage", "onAnyMessage"]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            logger.info(f"[WEBHOOK] WPPConnect webhook registered: {resp.status_code} — {webhook_url}")
    except Exception as exc:
        logger.warning(f"[WEBHOOK] Could not register WPPConnect webhook (WPPConnect may not be running): {exc}")


async def _auto_purge_loop():
    """Delete old occurrences/metrics/issues once a day."""
    PURGE_INTERVAL_SECONDS = 24 * 60 * 60
    # Wait 5 minutes after startup before first purge
    await asyncio.sleep(5 * 60)
    while True:
        try:
            cfg = load_config()
            await purge_old_events(cfg.error_retention_days)
            logger.info(f"[PURGE] Old events purged (retention={cfg.error_retention_days}d).")
        except Exception as exc:
            logger.error(f"[PURGE] failed: {exc}")
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Start background polling tasks
    asyncio.create_task(poll_loop())
    asyncio.create_task(log_poll_loop())
    asyncio.create_task(alert_queue_loop())
    asyncio.create_task(spike_detection_loop())
    asyncio.create_task(rules_loop())
    asyncio.create_task(nightly_loop())
    asyncio.create_task(autofix_loop())
    asyncio.create_task(job_watchdog_loop())
    asyncio.create_task(_auto_purge_loop())
    asyncio.create_task(_register_wppconnect_webhook())
    logger.info("Sofia Monitor started.")
    yield
    logger.info("Sofia Monitor shutting down.")


app = FastAPI(
    title="Sofia Monitor",
    description="Sistema de monitoreo centralizado para tus apps",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _api_key_middleware(request: Request, call_next):
    """
    Optional API-key auth. Only enforced if SOFIA_API_KEY env var is set.
    Allows /api/ping, /api/ingest/event and /api/webhook/wppconnect without auth.
    """
    api_key = os.getenv("SOFIA_API_KEY")
    if not api_key:
        return await call_next(request)

    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    if any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {api_key}"
    # Also accept X-API-Key for legacy clients
    x_key = request.headers.get("X-API-Key", "")
    if auth_header == expected or x_key == api_key:
        return await call_next(request)
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


# API routes
app.include_router(health.router, prefix="/api")
app.include_router(events.router, prefix="/api")
app.include_router(ingest.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(config.router, prefix="/api")
app.include_router(webhook.router, prefix="/api")
app.include_router(restore.router, prefix="/api")
app.include_router(nightly.router, prefix="/api")
app.include_router(autonomy.router, prefix="/api")


@app.get("/api/ping")
async def ping():
    return {"status": "ok", "service": "sofia-monitor"}


# Serve frontend build (React SPA)
FRONTEND_DIST = Path(__file__).parent.parent.parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str):
        # Never intercept API routes — return 404 JSON instead of the SPA
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        index = FRONTEND_DIST / "index.html"
        return FileResponse(str(index))
