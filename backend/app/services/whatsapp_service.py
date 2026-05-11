"""
WhatsApp alert service via wppconnect.
Sends alerts to configured number with cooldown to prevent spam.
"""
import httpx
import logging
from datetime import datetime, timedelta
from typing import Dict
from app.models.config import AlertConfig

logger = logging.getLogger("sofia.whatsapp")

# In-memory cooldown tracker: {alert_key: last_sent_datetime}
_cooldown: Dict[str, datetime] = {}


def _cooldown_key(service_id: str, level: str) -> str:
    return f"{service_id}:{level}"


def _is_on_cooldown(key: str, cooldown_minutes: int) -> bool:
    last = _cooldown.get(key)
    if last is None:
        return False
    return datetime.utcnow() - last < timedelta(minutes=cooldown_minutes)


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

    phone = alert_cfg.whatsapp_number.replace("+", "") + "@c.us"
    url = f"{alert_cfg.wppconnect_url}/api/{alert_cfg.wppconnect_session}/send-message"
    headers = {"Authorization": f"Bearer {alert_cfg.wppconnect_token}"}
    payload = {"phone": phone, "message": text, "isGroup": False}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            _cooldown[key] = datetime.utcnow()
            logger.info(f"[WA] Alert sent for {service_id}: {level}")
            return True
    except Exception as exc:
        logger.error(f"[WA] Failed to send alert for {service_id}: {exc}")
        return False
