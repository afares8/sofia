"""
WhatsApp alert service via wppconnect.
Sends alerts to configured number with cooldown to prevent spam.
If WPPConnect is unreachable, alerts are queued in SQLite for later delivery.
"""
import asyncio
import httpx
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
from app.models.config import AlertConfig

logger = logging.getLogger("sofia.whatsapp")

# In-memory cooldown tracker: {alert_key: last_sent_datetime}
_cooldown: Dict[str, datetime] = {}
_hourly_sent: Dict[str, list[datetime]] = {}
_session_token: Optional[str] = None
_token_lock: Optional[asyncio.Lock] = None


async def get_wppconnect_token(alert_cfg: AlertConfig, force_refresh: bool = False) -> str:
    global _session_token, _token_lock
    if _session_token and not force_refresh:
        return _session_token
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    async with _token_lock:
        if _session_token and not force_refresh:
            return _session_token
        secret_key = alert_cfg.wppconnect_secret_key or alert_cfg.wppconnect_token
        if not secret_key:
            raise RuntimeError("WPPConnect secret key is not configured")
        url = (
            f"{alert_cfg.wppconnect_url}/api/{alert_cfg.wppconnect_session}/"
            f"{secret_key}/generate-token"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url)
            resp.raise_for_status()
            token = str(resp.json().get("token") or "")
        if not token:
            raise RuntimeError("WPPConnect generate-token returned no token")
        _session_token = token
        return token


async def get_auth_headers(alert_cfg: AlertConfig, force_refresh: bool = False) -> dict[str, str]:
    token = await get_wppconnect_token(alert_cfg, force_refresh=force_refresh)
    return {"Authorization": f"Bearer {token}"}


async def get_connection_status(alert_cfg: AlertConfig) -> dict:
    url = (
        f"{alert_cfg.wppconnect_url}/api/{alert_cfg.wppconnect_session}/"
        "check-connection-session"
    )
    headers = await get_auth_headers(alert_cfg)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 401:
            headers = await get_auth_headers(alert_cfg, force_refresh=True)
            resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


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


class DeliveryUncertainError(RuntimeError):
    pass


def _delivery_uncertain(exc: Exception) -> bool:
    return isinstance(exc, DeliveryUncertainError)


def _delivery_error(exc: Exception) -> str:
    return f"delivery unknown; automatic retry disabled: {type(exc).__name__}: {exc}" if _delivery_uncertain(exc) else str(exc)


def _service_is_up(service_id: str) -> bool:
    """True si el servicio monitoreado ya se recupero (para cancelar un
    correo diferido: si solo era un reinicio, no se avisa por correo)."""
    from app.services import health_service
    status = health_service.get_status(service_id)
    return bool(status and status.status == "up")


async def _post_message(alert_cfg: AlertConfig, phone: str, text: str) -> bool:
    """Single HTTP POST to WPPConnect. Returns True on success."""
    url = f"{alert_cfg.wppconnect_url}/api/{alert_cfg.wppconnect_session}/send-message"
    headers = await get_auth_headers(alert_cfg)
    payload = {"phone": phone, "message": text, "isGroup": False}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError) as exc:
            raise DeliveryUncertainError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 401:
            headers = await get_auth_headers(alert_cfg, force_refresh=True)
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError) as exc:
                raise DeliveryUncertainError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 500:
            raise DeliveryUncertainError(f"WPPConnect HTTP {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        try:
            result = resp.json()
        except ValueError as exc:
            raise DeliveryUncertainError("WPPConnect returned invalid JSON") from exc
        if isinstance(result, dict) and str(result.get("status", "")).lower() in ("disconnected", "connected"):
            raise RuntimeError(f"WPPConnect rejected message: {result}")
        if not isinstance(result, dict) or str(result.get("status", "")).lower() != "success" or not result.get("response") or result.get("error"):
            raise DeliveryUncertainError(f"WPPConnect did not confirm success: {result}")
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
        logger.warning(f"[WA] send_message failed: {_delivery_error(exc)}")
        from app.services import db_service
        try:
            await db_service.enqueue_alert(target_phone, text, delivery_uncertain=_delivery_uncertain(exc), error=_delivery_error(exc))
        except Exception as enq_exc:
            logger.error(f"[WA] enqueue_alert failed: {enq_exc}")
        # Fallback: notify via email so the team is aware even when WhatsApp is down.
        from app.services import email_alert_service
        email_alert_service.send_fallback_email(
            subject="[Sofia] Entrega WhatsApp desconocida" if _delivery_uncertain(exc) else "[Sofia] Alerta WhatsApp no entregada",
            body=("Entrega desconocida; pudo haber llegado. No se reenviará automáticamente." if _delivery_uncertain(exc) else "No se pudo enviar el siguiente mensaje por WhatsApp.") + f"\n\n{text}\n\nError: {_delivery_error(exc)}",
        )
        return False


async def send_alert(
    alert_cfg: AlertConfig,
    service_name: str,
    service_id: str,
    level: str,
    message: str,
    detail: str = "",
    defer_email_seconds: int = 0,
) -> bool:
    """Envía una alerta por WhatsApp. Si falla y `defer_email_seconds` > 0,
    el correo de respaldo se programa para más tarde y se cancela si el
    servicio ya se recuperó (p. ej. un reinicio rápido de WPPConnect)."""
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
        text += f"*Detalle:* {detail[:1500]}\n"
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
        logger.error(f"[WA] Failed to send alert for {service_id}: {_delivery_error(exc)}")
        from app.services import db_service
        try:
            await db_service.enqueue_alert(phone, text, delivery_uncertain=_delivery_uncertain(exc), error=_delivery_error(exc))
            # apply cooldown even when queued, otherwise the queue gets spammed
            _cooldown[key] = datetime.utcnow()
        except Exception as enq_exc:
            logger.error(f"[WA] enqueue_alert failed: {enq_exc}")
        # Fallback: notify via email so the team is aware even when WhatsApp is down.
        from app.services import email_alert_service
        subject = f"[Sofia] {level} — {service_name} ({service_id})"
        body = ("Entrega desconocida; pudo haber llegado. No se reenviará automáticamente." if _delivery_uncertain(exc) else "La alerta no pudo enviarse por WhatsApp y fue encolada.") + f"\n\n{text}\n\nError: {_delivery_error(exc)}"
        if defer_email_seconds > 0:
            email_alert_service.schedule_fallback_email(
                subject, body, delay_seconds=defer_email_seconds,
                cancel_check=lambda: _service_is_up(service_id),
                key=f"deferred:{service_id}")
        else:
            email_alert_service.send_fallback_email(subject=subject, body=body)
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
            uncertain = _delivery_uncertain(exc)
            await db_service.mark_alert_failed(row["id"], _delivery_error(exc), delivery_uncertain=uncertain)
            if uncertain:
                logger.error(f"[WA] queue id={row['id']}: {_delivery_error(exc)}")
                from app.services import email_alert_service
                email_alert_service.send_fallback_email(
                    subject="[Sofia] Entrega WhatsApp desconocida",
                    body=f"Entrega desconocida; pudo haber llegado. No se reenviará automáticamente.\n\n{row['message']}\n\nError: {_delivery_error(exc)}",
                )
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
