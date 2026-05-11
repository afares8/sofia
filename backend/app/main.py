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

from app.routers import health, events, ingest, logs, config, webhook, restore
from app.services.db_service import init_db
from app.services.health_service import poll_loop
from app.services.log_service import log_poll_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sofia")


async def _register_wppconnect_webhook():
    """Register Sofia's webhook URL in WPPConnect so it receives incoming messages."""
    import asyncio, httpx
    from app.services.config_service import load_config
    await asyncio.sleep(5)  # Wait for WPPConnect to be ready
    cfg = load_config()
    webhook_url = "http://192.168.0.123:5180/api/webhook/wppconnect"
    url = f"{cfg.alerts.wppconnect_url}/api/{cfg.alerts.wppconnect_session}/webhook"
    headers = {"Authorization": f"Bearer {cfg.alerts.wppconnect_token}"}
    payload = {"webhook": webhook_url, "events": ["onMessage", "onAnyMessage"]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            logger.info(f"[WEBHOOK] WPPConnect webhook registered: {resp.status_code} — {webhook_url}")
    except Exception as exc:
        logger.warning(f"[WEBHOOK] Could not register WPPConnect webhook (WPPConnect may not be running): {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Start background polling tasks
    asyncio.create_task(poll_loop())
    asyncio.create_task(log_poll_loop())
    asyncio.create_task(_register_wppconnect_webhook())
    logger.info("Sofia Monitor started.")
    yield
    logger.info("Sofia Monitor shutting down.")


app = FastAPI(
    title="Sofia Monitor",
    description="Sistema de monitoreo centralizado para tus apps",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(health.router, prefix="/api")
app.include_router(events.router, prefix="/api")
app.include_router(ingest.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(config.router, prefix="/api")
app.include_router(webhook.router, prefix="/api")
app.include_router(restore.router, prefix="/api")


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
