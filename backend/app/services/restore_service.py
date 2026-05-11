"""
Restore service - handles WhatsApp-triggered service restoration via Devin.

Flow:
  1. health_service detects DOWN → calls notify_down_with_restore_prompt()
  2. Sofia sends WA: "🔴 <name> caído. Responde *SI <ID>* para restaurar."
  3. User replies "SI MAYOR" (or SI PACKING, SI PANTALLA)
  4. wpp_webhook router calls handle_incoming_message(sender, text)
  5. If valid: launches `devin -p --permission-mode dangerous "<prompt>"` with
     full context about the service, how it starts, what to check, etc.
  6. Devin diagnoses the problem, kills orphans, restarts the service,
     verifies health (+ DIAPI for mayor/packing), and reports result.
  7. Sofia reads Devin's output and reports back via WhatsApp.

Rules:
  - Only accepts commands from whatsapp_number in config
  - Format: "SI <service_id>" (case-insensitive) or "NO"
  - No retry loop: one Devin session per confirmation, report result
  - Timeout: 5 minutes to confirm, 10 minutes for Devin to finish
  - For mayor/packing: Devin explicitly verifies SAP DIAPI middleware
"""
import asyncio
import logging
import subprocess
import httpx
import shutil
from datetime import datetime, timedelta
from typing import Dict, Optional
from pathlib import Path

from app.models.restore import PendingRestore, RestoreStatus
from app.services.config_service import load_config
from app.services.health_service import get_status, check_service

logger = logging.getLogger("sofia.restore")

# In-memory store of pending/active restores keyed by service_id
_pending: Dict[str, PendingRestore] = {}

# Confirmation timeout: user must reply within this many minutes
CONFIRM_TIMEOUT_MINUTES = 5

# How long to wait for Devin to finish (generous — Devin may need to diagnose)
DEVIN_TIMEOUT_SECONDS = 600   # 10 minutes

# DIAPI health endpoint (Mayor and Packing use SAP middleware on port 9000)
DIAPI_HEALTH_URL = "http://localhost:9000/api/Health/Ping"
DIAPI_SERVICES = {"mayor", "packing"}

# Per-service context that Devin receives as part of the prompt
SERVICE_CONTEXT = {
    "mayor": {
        "port": 8075,
        "health_url": "http://192.168.0.123:8075/health",
        "work_dir": r"D:\mayor\backend",
        "start_cmd": r"C:\Users\ahmed\AppData\Local\Programs\Python\Python312\python.exe run.py",
        "extra": (
            "Mayor also manages a SAP DI API middleware (.NET) that starts automatically "
            "when the backend starts. After Mayor is up, verify the DIAPI middleware at "
            "http://localhost:9000/api/Health/Ping responds with HTTP 200. "
            "The middleware executable is at "
            r"D:\mayor\sap-diapi-middleware\SapDiApiMiddleware\bin\x86\Release\net48\SapDiApiMiddleware.exe "
            "(also check Debug path if Release doesn't exist). "
            "If DIAPI is not responding, try starting SapDiApiMiddleware.exe directly."
        ),
    },
    "packing": {
        "port": 8100,
        "health_url": "http://192.168.0.123:8100/health",
        "work_dir": r"D:\packing\backend",
        "start_cmd": "python run.py",
        "extra": (
            "Packing also uses the SAP DI API middleware. After Packing is up, verify "
            "DIAPI at http://localhost:9000/api/Health/Ping responds. "
            "If not responding, check if Mayor already started it (they share the same DIAPI process)."
        ),
    },
    "pantalla": {
        "port": 8000,
        "health_url": "http://192.168.0.123:8000/health",
        "work_dir": r"D:\Pantalla\backend",
        "start_cmd": "poetry run python run.py",
        "extra": "Pantalla is a display/dashboard service. No special middleware required.",
    },
}


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


def _build_devin_prompt(service_id: str, service_name: str) -> str:
    """Build a rich, contextual prompt for Devin to diagnose and restore a service."""
    ctx = SERVICE_CONTEXT.get(service_id, {})
    port      = ctx.get("port", "unknown")
    health    = ctx.get("health_url", f"http://192.168.0.123:{port}/health")
    work_dir  = ctx.get("work_dir", f"D:\\{service_id}\\backend")
    start_cmd = ctx.get("start_cmd", "python run.py")
    extra     = ctx.get("extra", "")

    return f"""You are being asked to diagnose and restore the {service_name} backend service which is currently DOWN.

## Your mission
Diagnose why the service is down, fix the problem, restart it, and confirm it is healthy.

## Service details
- Service name: {service_name}
- Working directory: {work_dir}
- Start command (run from working directory): {start_cmd}
- Health check URL: {health}
- Port: {port}

## Step-by-step instructions

### 1. Diagnose
- Check what process (if any) is occupying port {port}: `netstat -ano | findstr :{port}`
- Check the last 50 lines of the log file if it exists (look in {work_dir}\\logs\\ for app.log, error.log, out.log)
- Try to understand WHY the service is down (crash, port conflict, import error, missing env var, etc.)

### 2. Clean up orphan processes
- Kill any process holding port {port} using taskkill /F /PID <pid>
- Wait 2-3 seconds after killing

### 3. Start the service
- Change directory to: {work_dir}
- Run: {start_cmd}
- Run it as a background process so you can continue monitoring (use Start-Process in PowerShell or start a background job)

### 4. Verify health
- Poll {health} every 5 seconds for up to 3 minutes
- The service is healthy when it returns HTTP status < 500
- If it doesn't come up in 3 minutes, check the logs again for startup errors and try to fix them

{f"### 5. Additional checks{chr(10)}{extra}" if extra else ""}

## Rules
- Work autonomously — do not ask questions, just proceed
- If you encounter an error (missing dependency, config issue, etc.), fix it and retry
- At the end of your work, output a clear summary line starting with either:
  - RESTORE_SUCCESS: <brief description of what you did>
  - RESTORE_FAILED: <brief description of what went wrong>

The output line is machine-read by Sofia Monitor to report the result via WhatsApp.
"""


async def _run_restore(service_id: str) -> None:
    """Launch a Devin session to diagnose and restore the service."""
    restore = _pending.get(service_id)
    if not restore:
        return

    restore.status = RestoreStatus.RUNNING

    # Locate devin binary
    devin_bin = shutil.which("devin") or shutil.which("devin.exe")
    if not devin_bin:
        msg = "Devin CLI no encontrado en PATH."
        logger.error(f"[RESTORE] {msg}")
        restore.status = RestoreStatus.FAILED
        restore.finished_at = datetime.utcnow()
        restore.result_message = msg
        await _send_simple_message(f"❌ {msg} Instalar Devin CLI para restauración inteligente.")
        return

    await _send_simple_message(
        f"🤖 *Devin* está diagnosticando y restaurando *{restore.service_name}*...\n"
        f"_Puede tomar hasta {DEVIN_TIMEOUT_SECONDS // 60} minutos. Te avisaré cuando termine._"
    )

    prompt = _build_devin_prompt(service_id, restore.service_name)
    logger.info(f"[RESTORE] Launching Devin session for {service_id}")

    try:
        # Run devin in non-interactive print mode with dangerous permissions
        # asyncio.create_subprocess_exec lets us capture stdout without blocking
        proc = await asyncio.create_subprocess_exec(
            devin_bin, "--print", "--permission-mode", "dangerous", "--", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=SERVICE_CONTEXT.get(service_id, {}).get("work_dir", "D:\\"),
        )

        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=DEVIN_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            msg = f"Devin tardó más de {DEVIN_TIMEOUT_SECONDS // 60} minutos y fue cancelado."
            logger.error(f"[RESTORE] {msg}")
            restore.status = RestoreStatus.FAILED
            restore.finished_at = datetime.utcnow()
            restore.result_message = msg
            await _send_simple_message(
                f"⏰ *{restore.service_name}*: Devin tardó demasiado y fue cancelado.\n"
                f"Revisar manualmente."
            )
            return

        output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        # Keep last 2000 chars for storage/display
        restore.devin_output = output[-2000:] if len(output) > 2000 else output
        logger.info(f"[RESTORE] Devin finished for {service_id}. Exit={proc.returncode}")
        logger.debug(f"[RESTORE] Devin output tail:\n{restore.devin_output[-500:]}")

    except Exception as exc:
        msg = f"Error lanzando Devin: {exc}"
        logger.error(f"[RESTORE] {msg}")
        restore.status = RestoreStatus.FAILED
        restore.finished_at = datetime.utcnow()
        restore.result_message = msg
        await _send_simple_message(f"❌ Error al lanzar Devin para *{restore.service_name}*: {exc}")
        return

    # Parse Devin's summary line
    output_lines = output.splitlines()
    summary_line = next(
        (l for l in reversed(output_lines) if l.startswith("RESTORE_SUCCESS:") or l.startswith("RESTORE_FAILED:")),
        None
    )

    # Also do an independent health check to verify
    cfg = load_config()
    svc_config = next((s for s in cfg.services if s.id == service_id), None)
    came_up = False
    if svc_config:
        try:
            status = await check_service(svc_config)
            came_up = status.status == "up"
        except Exception:
            came_up = False

    diapi_ok = True
    if came_up and service_id in DIAPI_SERVICES:
        diapi_ok = await _check_diapi()

    restore.finished_at = datetime.utcnow()
    elapsed = int((restore.finished_at - restore.confirmed_at).total_seconds())

    if came_up and diapi_ok:
        restore.status = RestoreStatus.SUCCESS
        summary = summary_line.replace("RESTORE_SUCCESS:", "").strip() if summary_line and "SUCCESS" in summary_line else "Servicio restaurado."
        restore.result_message = summary
        msg = f"✅ *{restore.service_name}* restaurado en {elapsed}s.\n_{summary}_"
        if service_id in DIAPI_SERVICES:
            msg += "\n✅ Middleware SAP DIAPI respondiendo."
        await _send_simple_message(msg)
    elif came_up and not diapi_ok:
        restore.status = RestoreStatus.FAILED
        restore.result_message = "Servicio levantó pero DIAPI no responde."
        await _send_simple_message(
            f"⚠️ *{restore.service_name}* levantó pero el middleware SAP DIAPI no responde.\n"
            f"Revisar manualmente."
        )
    else:
        restore.status = RestoreStatus.FAILED
        summary = summary_line.replace("RESTORE_FAILED:", "").strip() if summary_line and "FAILED" in summary_line else "El servicio no respondió al health check."
        restore.result_message = summary
        await _send_simple_message(
            f"❌ *{restore.service_name}* no levantó tras {elapsed}s.\n"
            f"_{summary}_\n"
            f"Revisar logs manualmente."
        )
        logger.error(f"[RESTORE] {service_id} failed. Devin summary: {summary}")


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
