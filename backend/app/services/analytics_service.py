"""
Analytics - error rate aggregation and spike detection.
Spike detection compares the last hour's error rate against a 24h baseline.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from app.services import db_service, whatsapp_service
from app.services.config_service import load_config

logger = logging.getLogger("sofia.analytics")

# How often the spike detector runs.
SPIKE_LOOP_INTERVAL = 5 * 60

# Cooldown between spike alerts for the same service (no more than 1/30 min).
SPIKE_COOLDOWN_MINUTES = 30
_spike_cooldown: Dict[str, datetime] = {}


async def get_error_rate(service_id: Optional[str] = None, window_minutes: int = 60) -> int:
    """Count of ERROR/CRITICAL occurrences in the time window."""
    return await db_service.get_error_count(service_id, window_minutes)


async def get_error_rates_by_service(window_minutes: int = 60) -> Dict[str, int]:
    cfg = load_config()
    out: Dict[str, int] = {}
    for svc in cfg.services:
        out[svc.id] = await get_error_rate(svc.id, window_minutes)
    return out


async def detect_spike(service_id: str, multiplier: float = 3.0) -> bool:
    """
    Compare last hour's error rate against the average of the prior 24h.
    Returns True if last_hour > multiplier * baseline_hourly_avg AND >= 3 errors.
    """
    last_hour = await get_error_rate(service_id, window_minutes=60)
    if last_hour < 3:
        return False  # avoid false positives on tiny absolute counts

    baseline_24h = await get_error_rate(service_id, window_minutes=24 * 60)
    if baseline_24h <= 0:
        return False
    # exclude the last hour from the baseline so we compare against history
    baseline_excluding_last = max(0, baseline_24h - last_hour)
    baseline_avg = baseline_excluding_last / 23.0 if baseline_excluding_last else 0
    if baseline_avg <= 0:
        # No baseline yet — only flag if absolute count is high
        return last_hour >= 10
    return last_hour >= multiplier * baseline_avg


async def spike_detection_loop():
    """Background task that checks for error spikes every few minutes."""
    logger.info("[ANALYTICS] Spike detection loop started.")
    # Avoid alerting at startup before metrics warm up.
    await asyncio.sleep(60)
    while True:
        try:
            cfg = load_config()
            for svc in cfg.services:
                if not svc.enabled:
                    continue
                try:
                    spiked = await detect_spike(svc.id, multiplier=3.0)
                except Exception as exc:
                    logger.error(f"[ANALYTICS] spike detect failed for {svc.id}: {exc}")
                    continue
                if not spiked:
                    continue
                # Cooldown
                last = _spike_cooldown.get(svc.id)
                if last and datetime.utcnow() - last < timedelta(minutes=SPIKE_COOLDOWN_MINUTES):
                    continue
                _spike_cooldown[svc.id] = datetime.utcnow()
                count = await get_error_rate(svc.id, 60)
                logger.warning(f"[ANALYTICS] Spike detected for {svc.id}: {count} errors in 1h")
                await whatsapp_service.send_alert(
                    cfg.alerts, svc.name, svc.id, "WARNING",
                    f"📈 Spike de errores en {svc.name}",
                    f"{count} errores en la última hora (>3x del promedio).",
                )
        except Exception as exc:
            logger.error(f"[ANALYTICS] loop iteration failed: {exc}")
        await asyncio.sleep(SPIKE_LOOP_INTERVAL)
