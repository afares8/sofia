"""Health check endpoints (services + Sofia self-health + metrics)."""
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query

from app.services import db_service
from app.services.config_service import load_config
from app.services.health_service import get_all_statuses, check_service
from app.models.event import ServiceStatus

router = APIRouter(prefix="/health", tags=["health"])

# Sofia self-health: process start time (used to compute uptime).
_SOFIA_START_TIME = time.time()


@router.get("/", response_model=List[ServiceStatus])
async def list_statuses():
    """Return cached status for all services."""
    return list(get_all_statuses().values())


@router.post("/check/{service_id}", response_model=ServiceStatus)
async def force_check(service_id: str):
    """Immediately re-check a specific service."""
    cfg = load_config()
    svc = next((s for s in cfg.services if s.id == service_id), None)
    if not svc:
        raise HTTPException(404, f"Service '{service_id}' not found")
    return await check_service(svc)


# ── Sofia self-health ────────────────────────────────────────────────────────


@router.get("/sofia")
async def sofia_self_health() -> Dict:
    """
    Return Sofia's own health (DB writable, memory, uptime, WPPConnect reachable).
    """
    db_ok = True
    db_error: Optional[str] = None
    try:
        # Trivial DB roundtrip — list 0 issues
        await db_service.get_issues(limit=1, since_hours=1)
    except Exception as exc:
        db_ok = False
        db_error = str(exc)

    # Memory — psutil is optional; fall back to resource if not present
    memory_mb: Optional[float] = None
    try:
        import resource  # noqa: WPS433 (stdlib, unix-only)
        ru = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is in kilobytes on Linux, bytes on Mac
        scale = 1024 if os.uname().sysname == "Darwin" else 1
        memory_mb = round(ru.ru_maxrss * scale / 1024 / 1024, 2)
    except Exception:
        memory_mb = None

    # WPP reachability (best-effort)
    cfg = load_config()
    wpp_ok = False
    wpp_error: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(
                f"{cfg.alerts.wppconnect_url}/api/{cfg.alerts.wppconnect_session}/status-session",
                headers={"Authorization": f"Bearer {cfg.alerts.wppconnect_token}"},
            )
            wpp_ok = resp.status_code < 500
    except Exception as exc:
        wpp_error = str(exc)

    # Last successful poll = newest metric timestamp
    last_poll: Optional[str] = None
    try:
        for svc in cfg.services:
            metrics = await db_service.get_metrics(svc.id, since_hours=1)
            if metrics:
                ts = metrics[-1]["timestamp"]
                if last_poll is None or ts > last_poll:
                    last_poll = ts
    except Exception:
        pass

    return {
        "ok": db_ok,
        "uptime_seconds": int(time.time() - _SOFIA_START_TIME),
        "db_ok": db_ok,
        "db_error": db_error,
        "memory_mb": memory_mb,
        "wpp_ok": wpp_ok,
        "wpp_error": wpp_error,
        "last_poll": last_poll,
        "now": datetime.utcnow().isoformat(),
    }


# ── Metrics ──────────────────────────────────────────────────────────────────


@router.get("/{service_id}/metrics")
async def service_metrics(
    service_id: str,
    since_hours: int = Query(24, ge=1, le=24 * 30),
):
    """Return all metric datapoints for the service over the window."""
    cfg = load_config()
    if not any(s.id == service_id for s in cfg.services):
        raise HTTPException(404, f"Service '{service_id}' not found")
    return await db_service.get_metrics(service_id, since_hours)


@router.get("/{service_id}/stats")
async def service_stats(
    service_id: str,
    since_hours: int = Query(24, ge=1, le=24 * 30),
):
    """Aggregate stats for the service: avg, p50/p95/p99, uptime%."""
    cfg = load_config()
    if not any(s.id == service_id for s in cfg.services):
        raise HTTPException(404, f"Service '{service_id}' not found")
    stats = await db_service.get_response_stats(service_id, since_hours)
    uptime = await db_service.get_uptime_percent(service_id, since_hours)
    return {**stats, "uptime_percent": uptime}


@router.get("/summary")
async def health_summary():
    """High-level summary for the dashboard: uptime + response time per service."""
    cfg = load_config()
    statuses = get_all_statuses()
    out = []
    for svc in cfg.services:
        u24 = await db_service.get_uptime_percent(svc.id, 24)
        u7d = await db_service.get_uptime_percent(svc.id, 24 * 7)
        stats = await db_service.get_response_stats(svc.id, 24)
        status_obj = statuses.get(svc.id)
        out.append({
            "service_id": svc.id,
            "service_name": svc.name,
            "uptime_24h": u24,
            "uptime_7d": u7d,
            "avg_response_ms": stats.get("avg"),
            "p95_response_ms": stats.get("p95"),
            "current_status": status_obj.status if status_obj else "unknown",
            "enabled": svc.enabled,
        })
    return out
