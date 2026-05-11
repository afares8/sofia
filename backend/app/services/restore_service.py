"""
Restore service - handles WhatsApp-triggered service restoration.

Flow:
  1. health_service detects DOWN → calls notify_down_with_restore_prompt()
  2. Sofia sends WA: "🔴 <name> caído. Responde *SI <ID>* para restaurar."
  3. User replies "SI MAYOR" (or SI PACKING, SI PANTALLA)
  4. wpp_webhook router calls handle_incoming_message(sender, text)
  5. If valid: runs restore script, waits for health, reports result

Rules:
  - Only accepts commands from whatsapp_number in config
  - Format: "SI <service_id>" (case-insensitive) or "NO"
  - Only acts if service is currently in "down" state
  - No retry loop: one attempt, report success or failure
  - Timeout: 5 minutes for user to confirm, 3 minutes for service to come up
  - For mayor/packing: also verifies DIAPI middleware is up
"""
import asyncio
import logging
import subprocess
import httpx
from datetime import datetime, timedelta
from typing import Dict, Optional
from pathlib import Path

from app.models.restore import PendingRestore, RestoreStatus
from app.services.config_service import load_config
from app.services import whatsapp_service
from app.services.health_service import get_status, check_service

logger = logging.getLogger("sofia.restore")

# In-memory store of pending/active restores keyed by service_id
_pending: Dict[str, PendingRestore] = {}

# Confirmation timeout: user must reply within this many minutes
CONFIRM_TIMEOUT_MINUTES = 5

# How long to wait for service to come back up after running the script
RESTORE_TIMEOUT_SECONDS = 180

# DIAPI health endpoint (Mayor and Packing use SAP middleware on port 9000)
DIAPI_HEALTH_URL = "http://localhost:9000/api/Health/Ping"
DIAPI_SERVICES = {"mayor", "packing"}

# Base64-encoded PowerShell restore commands for each service
# These mirror exactly the same watchdog pattern used in the .bat startup files
# Script path: D:/sofia/backend/scripts/restore_<service_id>.ps1
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def get_all_pending() -> Dict[str, PendingRestore]:
    """Return all pending/active restores (for UI display)."""
    return dict(_pending)


async def notify_down_with_restore_prompt(service_id: str, service_name: str) -> None:
    """
    Called by health_service when a service crosses the failure threshold.
    Sends WhatsApp alert with restore prompt and stores a PendingRestore.
    Only sends the prompt for restoreable services (mayor, packing, pantalla).
    """
    cfg = load_config()
    restoreable = {"mayor", "packing", "pantalla"}

    # Don't spam: if there's already a pending/running restore, skip
    existing = _pending.get(service_id)
    if existing and existing.status in (RestoreStatus.PENDING, RestoreStatus.RUNNING, RestoreStatus.CONFIRMED):
        logger.info(f"[RESTORE] Already a pending restore for {service_id}, skipping prompt.")
        return

    # Send alert with restore prompt only for restoreable services
    if service_id in restoreable:
        text = (
            f"🔴 *{service_name}* ha caído.\n\n"
            f"Responde *SI {service_id.upper()}* para que Devin lo restaure automáticamente,\n"
            f"o *NO* para ignorar.\n\n"
            f"_(Tienes {CONFIRM_TIMEOUT_MINUTES} minutos para responder)_"
        )
    else:
        text = (
            f"🔴 *{service_name}* ha caído.\n"
            f"Este servicio no tiene restauración automática configurada.\n"
            f"Revisar manualmente."
        )

    phone = cfg.alerts.whatsapp_number.replace("+", "") + "@c.us"
    url = f"{cfg.alerts.wppconnect_url}/api/{cfg.alerts.wppconnect_session}/send-message"
    headers = {"Authorization": f"Bearer {cfg.alerts.wppconnect_token}"}
    payload = {"phone": phone, "message": text, "isGroup": False}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload, headers=headers)
        logger.info(f"[RESTORE] Sent restore prompt for {service_id}")
    except Exception as exc:
        logger.error(f"[RESTORE] Failed to send restore prompt: {exc}")

    if service_id in restoreable:
        _pending[service_id] = PendingRestore(
            service_id=service_id,
            service_name=service_name,
            status=RestoreStatus.PENDING,
            requested_at=datetime.utcnow(),
        )
        # Start expiry task
        asyncio.create_task(_expire_pending(service_id))


async def _expire_pending(service_id: str) -> None:
    """Mark pending restore as expired if user doesn't confirm in time."""
    await asyncio.sleep(CONFIRM_TIMEOUT_MINUTES * 60)
    restore = _pending.get(service_id)
    if restore and restore.status == RestoreStatus.PENDING:
        restore.status = RestoreStatus.EXPIRED
        restore.finished_at = datetime.utcnow()
        restore.result_message = "Sin respuesta del usuario."
        logger.info(f"[RESTORE] Restore for {service_id} expired (no confirmation).")
        # Notify user
        await _send_simple_message(f"⏰ Sin respuesta — restauración de *{restore.service_name}* cancelada.")


async def handle_incoming_message(sender_phone: str, text: str) -> None:
    """
    Called by the webhook router when a WhatsApp message is received.
    Validates sender, parses command, and triggers restore if appropriate.
    """
    cfg = load_config()
    # Normalize sender: WPPConnect sends "50766662916@c.us" or "50766662916"
    authorized = cfg.alerts.whatsapp_number.replace("+", "").replace("@c.us", "")
    sender_normalized = sender_phone.replace("+", "").replace("@c.us", "").replace("@lid", "")

    if sender_normalized != authorized:
        logger.debug(f"[RESTORE] Ignoring message from unauthorized sender: {sender_phone}")
        return

    text = text.strip().upper()
    logger.info(f"[RESTORE] Received command from authorized sender: '{text}'")

    # Parse "NO" — cancel all pending
    if text == "NO":
        cancelled = []
        for sid, restore in list(_pending.items()):
            if restore.status == RestoreStatus.PENDING:
                restore.status = RestoreStatus.REJECTED
                restore.finished_at = datetime.utcnow()
                restore.result_message = "Cancelado por el usuario."
                cancelled.append(restore.service_name)
        if cancelled:
            await _send_simple_message(f"✅ Restauración cancelada para: {', '.join(cancelled)}")
        return

    # Parse "SI <service_id>"
    if text.startswith("SI "):
        service_id = text[3:].strip().lower()
        restore = _pending.get(service_id)

        if not restore:
            await _send_simple_message(f"⚠️ No hay restauración pendiente para *{service_id}*.")
            return
        if restore.status != RestoreStatus.PENDING:
            await _send_simple_message(f"⚠️ La restauración de *{restore.service_name}* ya está en estado: {restore.status.value}")
            return

        # Confirm and run
        restore.status = RestoreStatus.CONFIRMED
        restore.confirmed_at = datetime.utcnow()
        logger.info(f"[RESTORE] User confirmed restore for {service_id}")
        asyncio.create_task(_run_restore(service_id))
        return

    # Unknown command — just ignore silently (could be a normal conversation message)
    logger.debug(f"[RESTORE] Ignoring unrecognized command: '{text}'")


async def _run_restore(service_id: str) -> None:
    """Execute the restore script and wait for service to come back up."""
    restore = _pending.get(service_id)
    if not restore:
        return

    restore.status = RestoreStatus.RUNNING
    cfg = load_config()

    script_path = SCRIPTS_DIR / f"restore_{service_id}.ps1"
    if not script_path.exists():
        msg = f"❌ Script de restauración no encontrado: {script_path}"
        logger.error(f"[RESTORE] {msg}")
        restore.status = RestoreStatus.FAILED
        restore.finished_at = datetime.utcnow()
        restore.result_message = msg
        await _send_simple_message(f"❌ No se encontró el script de restauración para *{restore.service_name}*.")
        return

    await _send_simple_message(
        f"⚙️ Ejecutando restauración de *{restore.service_name}*...\n"
        f"_Espera hasta {RESTORE_TIMEOUT_SECONDS // 60} minutos._"
    )

    logger.info(f"[RESTORE] Running script: {script_path}")
    try:
        # Run the PowerShell script (non-blocking, detached)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except Exception as exc:
        msg = f"Error al ejecutar script: {exc}"
        logger.error(f"[RESTORE] {msg}")
        restore.status = RestoreStatus.FAILED
        restore.finished_at = datetime.utcnow()
        restore.result_message = msg
        await _send_simple_message(f"❌ Error ejecutando restauración de *{restore.service_name}*: {exc}")
        return

    # Wait for service to come back up
    svc_config = next((s for s in cfg.services if s.id == service_id), None)
    deadline = datetime.utcnow() + timedelta(seconds=RESTORE_TIMEOUT_SECONDS)
    came_up = False

    while datetime.utcnow() < deadline:
        await asyncio.sleep(10)
        try:
            if svc_config:
                status = await check_service(svc_config)
                if status.status == "up":
                    came_up = True
                    break
        except Exception as exc:
            logger.debug(f"[RESTORE] Health check error during wait: {exc}")

    # For mayor/packing also verify DIAPI
    diapi_ok = True
    if came_up and service_id in DIAPI_SERVICES:
        diapi_ok = await _check_diapi()
        if not diapi_ok:
            logger.warning(f"[RESTORE] {service_id} came up but DIAPI middleware is not responding")

    restore.finished_at = datetime.utcnow()

    if came_up and diapi_ok:
        restore.status = RestoreStatus.SUCCESS
        restore.result_message = "Servicio restaurado correctamente."
        elapsed = int((restore.finished_at - restore.confirmed_at).total_seconds())
        msg = (
            f"✅ *{restore.service_name}* restaurado correctamente en {elapsed}s.\n"
        )
        if service_id in DIAPI_SERVICES:
            msg += "✅ Middleware SAP DIAPI también respondiendo.\n"
        await _send_simple_message(msg)
        logger.info(f"[RESTORE] {service_id} restored successfully in {elapsed}s")
    elif came_up and not diapi_ok:
        restore.status = RestoreStatus.FAILED
        restore.result_message = "Servicio levantó pero DIAPI middleware no responde."
        await _send_simple_message(
            f"⚠️ *{restore.service_name}* levantó pero el middleware SAP DIAPI no responde.\n"
            f"Verificar D:\\mayor\\sap-diapi-middleware manualmente."
        )
    else:
        restore.status = RestoreStatus.FAILED
        restore.result_message = f"El servicio no respondió en {RESTORE_TIMEOUT_SECONDS}s."
        await _send_simple_message(
            f"❌ *{restore.service_name}* no levantó en {RESTORE_TIMEOUT_SECONDS // 60} minutos.\n"
            f"Revisar manualmente."
        )
        logger.error(f"[RESTORE] {service_id} failed to come back up within timeout")


async def _check_diapi() -> bool:
    """Check if the SAP DIAPI middleware is responding."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(DIAPI_HEALTH_URL)
            return resp.status_code < 500
    except Exception:
        return False


async def _send_simple_message(text: str) -> None:
    """Send a plain WhatsApp message to the configured number."""
    cfg = load_config()
    if not cfg.alerts.whatsapp_enabled:
        return
    phone = cfg.alerts.whatsapp_number.replace("+", "") + "@c.us"
    url = f"{cfg.alerts.wppconnect_url}/api/{cfg.alerts.wppconnect_session}/send-message"
    headers = {"Authorization": f"Bearer {cfg.alerts.wppconnect_token}"}
    payload = {"phone": phone, "message": text, "isGroup": False}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload, headers=headers)
    except Exception as exc:
        logger.error(f"[RESTORE] Failed to send message: {exc}")
