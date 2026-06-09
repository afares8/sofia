"""
Health check service - polls each configured service periodically.

Status flow:
  unknown -> up          : first successful check
  up      -> restarting  : 1st...(threshold-1) consecutive failures (grace period)
  restarting -> up       : service recovered before threshold → no alert
  restarting -> down     : failures >= threshold → alert fired
  down    -> up          : recovery (no alert, just status change)

This means a uvicorn --reload or quick crash-restart never triggers a
WhatsApp alert as long as the service is back within:
  failure_threshold × poll_interval_seconds  (default: 3 × 30s = 90s)
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
    prev_failures = prev.consecutive_failures if prev else 0
    threshold = svc.failure_threshold  # default 3

    # --- perform HTTP check ---
    http_ok = False
    status_code = None
    response_ms = None
    try:
        start = asyncio.get_event_loop().time()
        async with httpx.AsyncClient(timeout=svc.timeout_seconds, verify=False, follow_redirects=True) as client:
            headers = {}
            if "21465" in svc.url or "wppconnect" in svc.id:
                cfg = load_config()
                headers["Authorization"] = f"Bearer {cfg.alerts.wppconnect_token}"
            resp = await client.get(svc.url, headers=headers)
            if resp.status_code == 404 and svc.url.rstrip("/").endswith("/health"):
                fallback_url = svc.url.rstrip("/") + "z"
                fallback_resp = await client.get(fallback_url, headers=headers)
                if fallback_resp.status_code < 500:
                    resp = fallback_resp
        response_ms = round((asyncio.get_event_loop().time() - start) * 1000, 1)
        status_code = resp.status_code
        http_ok = resp.status_code == svc.expected_status or resp.status_code < 500
    except Exception as exc:
        logger.warning(f"[HEALTH] {svc.name} unreachable: {exc}")

    # --- compute new consecutive_failures and status ---
    if http_ok:
        new_failures = 0
        new_status = "up"
    else:
        new_failures = prev_failures + 1
        if new_failures < threshold:
            # still within grace period — mark as restarting, not down
            new_status = "restarting"
        else:
            new_status = "down"

    status = ServiceStatus(
        id=svc.id,
        name=svc.name,
        status=new_status,
        status_code=status_code,
        response_ms=response_ms,
        enabled=svc.enabled,
        last_checked=datetime.utcnow(),
        last_seen_up=(datetime.utcnow() if http_ok else (prev.last_seen_up if prev else None)),
        consecutive_failures=new_failures,
    )

    # --- fire alert only when crossing the threshold (restarting → down) ---
    prev_status = prev.status if prev else "unknown"
    just_crossed_threshold = (
        new_status == "down"
        and prev_status in ("up", "restarting", "unknown")
        and new_failures == threshold   # exact moment of crossing
        and svc.enabled
    )

    if just_crossed_threshold:
        cfg = load_config()
        grace_secs = threshold * cfg.poll_interval_seconds
        event = ErrorEvent(
            service_id=svc.id,
            service_name=svc.name,
            level="CRITICAL",
            message=f"{svc.name} no responde (DOWN)",
            detail=(
                f"URL: {svc.url} — {new_failures} chequeos fallidos consecutivos "
                f"({grace_secs}s de gracia agotados)"
            ),
            source="passive",
            timestamp=datetime.utcnow(),
        )
        event_id = await db_service.insert_event(event)
        sent = await whatsapp_service.send_alert(
            cfg.alerts, svc.name, svc.id, "CRITICAL",
            f"{svc.name} no responde",
            f"URL: {svc.url} — caído por más de {grace_secs}s",
        )
        if sent:
            await db_service.mark_notified(event_id)
        # Trigger restore prompt for restoreable services
        from app.services import restore_service
        await restore_service.notify_down_with_restore_prompt(svc.id, svc.name)
        logger.warning(
            f"[HEALTH] {svc.name} DOWN after {new_failures} consecutive failures — alert sent"
        )
    elif new_status == "restarting" and prev_status == "up":
        logger.info(
            f"[HEALTH] {svc.name} failed check {new_failures}/{threshold} — "
            f"waiting (grace period, no alert yet)"
        )
    elif new_status == "up" and prev_status in ("restarting", "down"):
        logger.info(f"[HEALTH] {svc.name} recovered after {prev_failures} failure(s)")
        if prev_status == "down" and svc.enabled:
            cfg = load_config()
            await whatsapp_service.send_alert(
                cfg.alerts, svc.name, svc.id, "INFO",
                f"✅ {svc.name} recuperado",
                f"El servicio volvió a responder después de {prev_failures} fallo(s). URL: {svc.url}",
            )

    _status_cache[svc.id] = status

    # Record metric (response time + uptime sample)
    try:
        await db_service.record_metric(
            svc.id, response_ms, status_code, http_ok,
        )
    except Exception as exc:
        logger.debug(f"[HEALTH] record_metric failed for {svc.id}: {exc}")

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
