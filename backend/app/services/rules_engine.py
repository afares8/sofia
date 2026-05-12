"""
Alert rules engine - evaluates user-defined AlertRule entries every few minutes
against metrics and analytics. Sends a WhatsApp alert when a rule fires.

Condition types supported:
  - error_count      : threshold = max errors in window_minutes
  - response_ms      : threshold = max p95 response time (ms) in window_minutes
  - downtime_minutes : threshold = downtime minutes in window_minutes
  - spike            : threshold = multiplier vs 24h baseline error rate
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from app.models.config import AlertRule, ServiceConfig
from app.services import analytics_service, db_service, whatsapp_service
from app.services.config_service import load_config

logger = logging.getLogger("sofia.rules")

# Per-rule cooldown tracker keyed by (rule_id, service_id).
_cooldown: Dict[str, datetime] = {}

RULES_LOOP_INTERVAL = 2 * 60


def _cooldown_key(rule_id: str, service_id: Optional[str]) -> str:
    return f"{rule_id}:{service_id or 'all'}"


def _on_cooldown(rule: AlertRule, service_id: Optional[str]) -> bool:
    key = _cooldown_key(rule.id, service_id)
    last = _cooldown.get(key)
    if last is None:
        return False
    return datetime.utcnow() - last < timedelta(minutes=rule.cooldown_minutes)


def _mark_fired(rule: AlertRule, service_id: Optional[str]) -> None:
    _cooldown[_cooldown_key(rule.id, service_id)] = datetime.utcnow()


async def _evaluate_for_service(rule: AlertRule, svc: ServiceConfig) -> Optional[str]:
    """
    Evaluate a single rule for a single service.
    Returns an alert message if the rule fires, otherwise None.
    """
    if rule.condition_type == "error_count":
        count = await analytics_service.get_error_rate(svc.id, rule.window_minutes)
        if count >= rule.threshold:
            return (
                f"{int(count)} errores en los últimos {rule.window_minutes} minutos "
                f"(umbral {int(rule.threshold)})."
            )
        return None

    if rule.condition_type == "response_ms":
        stats = await db_service.get_response_stats(svc.id, max(1, rule.window_minutes // 60) or 1)
        p95 = stats.get("p95")
        if p95 is not None and p95 >= rule.threshold:
            return f"P95 response time = {p95}ms (umbral {int(rule.threshold)}ms)."
        return None

    if rule.condition_type == "downtime_minutes":
        # Approximate: count metric samples where is_up=0 and multiply by avg interval.
        cfg = load_config()
        interval = cfg.poll_interval_seconds or 30
        metrics = await db_service.get_metrics(svc.id, max(1, rule.window_minutes // 60) or 1)
        downs = sum(1 for m in metrics if not m["is_up"])
        downtime_min = round(downs * interval / 60.0, 2)
        if downtime_min >= rule.threshold:
            return f"Downtime ~{downtime_min} min en últimos {rule.window_minutes} min."
        return None

    if rule.condition_type == "spike":
        spiked = await analytics_service.detect_spike(svc.id, multiplier=rule.threshold or 3.0)
        if spiked:
            count = await analytics_service.get_error_rate(svc.id, 60)
            return f"Spike de errores: {count} en 1h (>{rule.threshold}x baseline)."
        return None

    logger.warning(f"[RULES] Unknown condition_type: {rule.condition_type}")
    return None


async def evaluate_rules() -> int:
    """Evaluate all enabled rules. Returns the number of alerts fired."""
    cfg = load_config()
    if not cfg.alert_rules:
        return 0
    fired = 0
    for rule in cfg.alert_rules:
        if not rule.enabled:
            continue
        # Pick the services this rule applies to.
        if rule.service_id:
            services = [s for s in cfg.services if s.id == rule.service_id and s.enabled]
        else:
            services = [s for s in cfg.services if s.enabled]

        for svc in services:
            if _on_cooldown(rule, svc.id):
                continue
            try:
                msg = await _evaluate_for_service(rule, svc)
            except Exception as exc:
                logger.error(f"[RULES] {rule.id} eval failed for {svc.id}: {exc}")
                continue
            if not msg:
                continue
            _mark_fired(rule, svc.id)
            fired += 1
            await whatsapp_service.send_alert(
                cfg.alerts, svc.name, svc.id, "WARNING",
                f"📋 Regla: {rule.name}", msg,
            )
            logger.info(f"[RULES] Fired '{rule.id}' for {svc.id}: {msg}")
    return fired


async def rules_loop():
    logger.info("[RULES] Rules engine loop started.")
    # Wait so we don't fire on startup before metrics warm up
    await asyncio.sleep(90)
    while True:
        try:
            await evaluate_rules()
        except Exception as exc:
            logger.error(f"[RULES] loop iteration failed: {exc}")
        await asyncio.sleep(RULES_LOOP_INTERVAL)
