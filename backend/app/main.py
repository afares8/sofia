"""
Sofia Monitor - Main FastAPI application.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from app.routers import health, events, ingest, logs, config
from app.services.db_service import init_db
from app.services.health_service import poll_loop
from app.services.log_service import log_poll_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sofia")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Start background polling tasks
    asyncio.create_task(poll_loop())
    asyncio.create_task(log_poll_loop())
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


@app.get("/api/ping")
async def ping():
    return {"status": "ok", "service": "sofia-monitor"}


# Serve frontend build (React SPA)
FRONTEND_DIST = Path(__file__).parent.parent.parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        index = FRONTEND_DIST / "index.html"
        return FileResponse(str(index))
