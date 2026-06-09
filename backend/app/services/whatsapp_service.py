"""
WhatsApp alert service via wppconnect.
Sends alerts to configured number with cooldown to prevent spam.
If WPPConnect is unreachable, alerts are queued in SQLite for later delivery.
"""
import httpx
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
from app.models.config import AlertConfig

logger = logging.getLogger("sofia.whatsapp")

# In-memory cooldown tracker: {alert_key: last_sent_datetime}
_cooldown: Dict[str, datetime] = {}
_hourly_sent: Dict[str, list[datetime]] = {}


def _cooldown_key(service_id: str, level: str) -> str:
    return f"{service_id}:{level}"


def _is_on_cooldown(key: str, cooldown_minutes: int) -> bool:
    last = _cooldown.get(key)
    if last is None:
        return False
    return datetime.utcnow() - last < timedelta(minutes=cooldown_minutes)


def _is_hourly_limited(phone: str, max_messages_per_hour: int) -> bool:
    if max_messages_per_hour <= 0:
        return False
    cutoff = datetime.utcnow() - timedelta(hours=1)
    recent = [ts for ts in _hourly_sent.get(phone, []) if ts >= cutoff]
    _hourly_sent[phone] = recent
    return len(recent) >= max_messages_per_hour


def _mark_hourly_sent(phone: str) -> None:
    _hourly_sent.setdefault(phone, []).append(datetime.utcnow())


def _format_phone(number: str) -> str:
    return number.replace("+", "").replace("@c.us", "") + "@c.us"


async def _post_message(alert_cfg: AlertConfig, phone: str, text: str) -> bool:
    """Single HTTP POST to WPPConnect. Returns True on success."""
    url = f"{alert_cfg.wppconnect_url}/api/{alert_cfg.wppconnect_session}/send-message"
    headers = {"Authorization": f"Bearer {alert_cfg.wppconnect_token}"}
    payload = {"phone": phone, "message": text, "isGroup": False}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
    return True


async def send_message(alert_cfg: AlertConfig, text: str, phone: Optional[str] = None) -> bool:
    """
    Plain WhatsApp message — no cooldown, no level formatting.
    On failure, enqueues the message in SQLite for the alert_queue worker.
    """
    if not alert_cfg.whatsapp_enabled:
        return False
    target_phone = _format_phone(phone or alert_cfg.whatsapp_number)
    if _is_hourly_limited(target_phone, alert_cfg.max_messages_per_hour):
        logger.warning(f"[WA] hourly limit reached for {target_phone}, dropping message.")
        return False
    try:
        await _post_message(alert_cfg, target_phone, text)
        _mark_hourly_sent(target_phone)
        return True
    except Exception as exc:
        logger.warning(f"[WA] send_message failed, queueing: {exc}")
        from app.services import db_service
        try:
            await db_service.enqueue_alert(target_phone, text)
        except Exception as enq_exc:
            logger.error(f"[WA] enqueue_alert failed: {enq_exc}")
        return False


async def send_alert(
    alert_cfg: AlertConfig,
    service_name: str,
    service_id: str,
    level: str,
    message: str,
    detail: str = "",
) -> bool:
    if not alert_cfg.whatsapp_enabled:
        return False

    key = _cooldown_key(service_id, level)
    if _is_on_cooldown(key, alert_cfg.cooldown_minutes):
        logger.debug(f"[WA] Alert for {service_id} on cooldown, skipping.")
        return False

    emoji = {"ERROR": "🔴", "CRITICAL": "🚨", "WARNING": "🟡"}.get(level, "ℹ️")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"{emoji} *Sofia Monitor*\n"
        f"*Servicio:* {service_name}\n"
        f"*Nivel:* {level}\n"
        f"*Mensaje:* {message}\n"
    )
    if detail:
        text += f"*Detalle:* {detail[:300]}\n"
    text += f"_🕐 {now}_"

    phone = _format_phone(alert_cfg.whatsapp_number)
    if _is_hourly_limited(phone, alert_cfg.max_messages_per_hour):
        logger.warning(f"[WA] hourly limit reached for {phone}, dropping alert for {service_id}: {level}")
        _cooldown[key] = datetime.utcnow()
        return False
    try:
        await _post_message(alert_cfg, phone, text)
        _mark_hourly_sent(phone)
        _cooldown[key] = datetime.utcnow()
        logger.info(f"[WA] Alert sent for {service_id}: {level}")
        return True
    except Exception as exc:
        logger.error(f"[WA] Failed to send alert for {service_id}, queueing: {exc}")
        from app.services import db_service
        try:
            await db_service.enqueue_alert(phone, text)
            # apply cooldown even when queued, otherwise the queue gets spammed
            _cooldown[key] = datetime.utcnow()
        except Exception as enq_exc:
            logger.error(f"[WA] enqueue_alert failed: {enq_exc}")
        return False


async def flush_queue(alert_cfg: AlertConfig, max_attempts: int = 10) -> int:
    """
    Try to send all queued alerts. Returns the number successfully delivered.
    Safe to call repeatedly from a background loop.
    """
    if not alert_cfg.whatsapp_enabled:
        return 0

    from app.services import db_service
    pending = await db_service.get_pending_alerts(max_attempts=max_attempts)
    delivered = 0
    for row in pending:
        if _is_hourly_limited(row["phone"], alert_cfg.max_messages_per_hour):
            logger.warning(f"[WA] hourly limit reached for {row['phone']}, pausing queue flush.")
            break
        try:
            await _post_message(alert_cfg, row["phone"], row["message"])
            _mark_hourly_sent(row["phone"])
            await db_service.mark_alert_sent(row["id"])
            delivered += 1
        except Exception as exc:
            await db_service.mark_alert_failed(row["id"], str(exc))
            # Stop trying for now — WPP probably still down
            logger.debug(f"[WA] queue flush halted at id={row['id']}: {exc}")
            break
    if delivered:
        logger.info(f"[WA] flushed {delivered} queued alert(s).")
    return delivered


async def send_to_escalation(alert_cfg: AlertConfig, text: str) -> int:
    """
    Send the same message to every number in escalation_numbers.
    Returns the count successfully sent.
    """
    if not alert_cfg.escalation_enabled or not alert_cfg.escalation_numbers:
        return 0
    sent = 0
    for number in alert_cfg.escalation_numbers:
        try:
            await _post_message(alert_cfg, _format_phone(number), text)
            sent += 1
        except Exception as exc:
            logger.warning(f"[WA] escalation to {number} failed: {exc}")
    return sent
