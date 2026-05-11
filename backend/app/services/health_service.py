"""
Health check service - polls each configured service periodically.
Stores state in-memory and fires alerts on status changes.
"""
import asyncio
import httpx
import logging
from datetime import datetime
from typing import Dict
from app.models.config import ServiceConfig
from app.models.event import ServiceStatus, ErrorEvent
from app.services import db_service, whatsapp_service
from app.services.config_service import load_config

logger = logging.getLogger("sofia.health")

# In-memory status cache
_status_cache: Dict[str, ServiceStatus] = {}


def get_all_statuses() -> Dict[str, ServiceStatus]:
    return dict(_status_cache)


def get_status(service_id: str) -> ServiceStatus | None:
    return _status_cache.get(service_id)


async def check_service(svc: ServiceConfig) -> ServiceStatus:
    prev = _status_cache.get(svc.id)
    status = ServiceStatus(
        id=svc.id,
        name=svc.name,
        enabled=svc.enabled,
        last_checked=datetime.utcnow(),
        last_seen_up=prev.last_seen_up if prev else None,
    )

    try:
        start = asyncio.get_event_loop().time()
        async with httpx.AsyncClient(timeout=svc.timeout_seconds) as client:
            # wppconnect needs auth header
            headers = {}
            if "21465" in svc.url or "wppconnect" in svc.id:
                cfg = load_config()
                headers["Authorization"] = f"Bearer {cfg.alerts.wppconnect_token}"
            resp = await client.get(svc.url, headers=headers)
        elapsed = (asyncio.get_event_loop().time() - start) * 1000
        status.response_ms = round(elapsed, 1)
        status.status_code = resp.status_code

        if resp.status_code == svc.expected_status or resp.status_code < 500:
            status.status = "up"
            status.last_seen_up = datetime.utcnow()
        else:
            status.status = "down"
    except Exception as exc:
        status.status = "down"
        status.status_code = None
        logger.warning(f"[HEALTH] {svc.name} unreachable: {exc}")

    # Detect transition down → alert
    was_up = (prev is None or prev.status == "up")
    now_down = status.status == "down"
    if was_up and now_down and svc.enabled:
        cfg = load_config()
        event = ErrorEvent(
            service_id=svc.id,
            service_name=svc.name,
            level="CRITICAL",
            message=f"{svc.name} no responde (DOWN)",
            detail=f"URL: {svc.url}",
            source="passive",
            timestamp=datetime.utcnow(),
        )
        event_id = await db_service.insert_event(event)
        sent = await whatsapp_service.send_alert(
            cfg.alerts, svc.name, svc.id, "CRITICAL",
            f"{svc.name} no responde", f"URL: {svc.url}"
        )
        if sent:
            await db_service.mark_notified(event_id)

    _status_cache[svc.id] = status
    return status


async def poll_loop():
    """Background task: poll all enabled services every N seconds."""
    logger.info("[HEALTH] Poll loop started.")
    while True:
        cfg = load_config()
        for svc in cfg.services:
            if svc.enabled:
                try:
                    await check_service(svc)
                except Exception as exc:
                    logger.error(f"[HEALTH] Error checking {svc.id}: {exc}")
        await asyncio.sleep(cfg.poll_interval_seconds)
