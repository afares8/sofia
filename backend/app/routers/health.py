"""Health check endpoints (services + Sofia self-health + metrics)."""
import asyncio
import os
import subprocess
import time
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.services import db_service, whatsapp_service, noise_service, drift_service
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


@router.get("/tunnel")
async def sofia_tunnel_health() -> JSONResponse:
    """
    Health of the SofiaTunnel reverse SSH tunnel to the ElenaTec droplet.

    Returns 200 only when BOTH:
    - the SofiaTunnel Windows service is RUNNING, and
    - an ESTABLISHED TCP connection to the droplet (198.211.99.4:22) exists.

    Otherwise 503 so the service entry 'sofiatunnel' in config can detect
    a dead tunnel and trigger restore_sofiatunnel.ps1 (ACL repair + restart).
    """

    def _check() -> Dict:
        info: Dict = {"service_running": False, "ssh_established": False}
        try:
            out = subprocess.run(
                ["sc.exe", "query", "SofiaTunnel"],
                capture_output=True, text=True, timeout=10,
            )
            info["service_running"] = (
                out.returncode == 0 and "RUNNING" in out.stdout
            )
        except Exception as exc:
            info["service_error"] = str(exc)
        try:
            out = subprocess.run(
                ["netstat", "-n"],
                capture_output=True, text=True, timeout=10,
            )
            info["ssh_established"] = any(
                "198.211.99.4:22" in line and "ESTABLISHED" in line
                for line in out.stdout.splitlines()
            )
        except Exception as exc:
            info["netstat_error"] = str(exc)
        return info

    info = await asyncio.to_thread(_check)
    ok = info["service_running"] and info["ssh_established"]
    info["ok"] = ok
    return JSONResponse(info, status_code=200 if ok else 503)


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

    # Memory — psutil RSS (works on Windows and Unix)
    memory_mb: Optional[float] = None
    try:
        import psutil
        # rss is in bytes on all platforms
        memory_mb = round(
            psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024),
            2,
        )
    except Exception:
        memory_mb = None

    # WPP reachability (best-effort)
    cfg = load_config()
    wpp_ok = False
    wpp_error: Optional[str] = None
    try:
        wpp_status = await whatsapp_service.get_connection_status(cfg.alerts)
        raw_status = wpp_status.get("status")
        wpp_ok = raw_status is True or str(raw_status).upper() in {
            "TRUE", "CONNECTED", "1", "YES"
        }
        if not wpp_ok:
            wpp_error = str(wpp_status.get("message") or "Disconnected")
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

    # Droplet docker monitor + local host monitor liveness
    docker_monitor: Dict = {}
    local_monitor: Dict = {}
    try:
        from app.services import droplet_docker_service
        docker_monitor = droplet_docker_service.get_monitor_status()
    except Exception:
        pass
    try:
        from app.services import local_host_service
        local_monitor = local_host_service.get_monitor_status()
    except Exception:
        pass

    log_monitor: Dict = {}
    try:
        from app.services import log_service
        log_monitor = log_service.get_monitor_status()
    except Exception:
        pass

    automation = {"codex_available": False}
    try:
        from app.services import codex_cli_service
        automation = {"codex_available": codex_cli_service.codex_available()}
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
        "docker_monitor": docker_monitor,
        "local_monitor": local_monitor,
        "log_monitor": log_monitor,
        "noise": noise_service.get_stats(),
        "deploy_drift": drift_service.check(),
        "automation": automation,
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
