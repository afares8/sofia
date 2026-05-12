"""
Restore service - handles WhatsApp-triggered service restoration via Devin.

Flow:
  1. health_service detects DOWN → calls notify_down_with_restore_prompt()
  2. If the service has auto_restore=True, Sofia restarts it without asking.
     Otherwise Sofia sends WA: "🔴 <name> caído. Responde *SI <ID>* para restaurar."
  3. User replies "SI MAYOR" (or SI PACKING, SI PANTALLA)
  4. wpp_webhook router calls handle_incoming_message(sender, text)
  5. If valid: launches `devin -p --permission-mode dangerous "<prompt>"` with
     full context about the service, how it starts, what to check, etc.
     If Devin is not available, the matching PowerShell script in
     `app/scripts/restore_<service_id>.ps1` is executed as a fallback.
  6. Sofia reads the result and reports it via WhatsApp.

Rules:
  - Only accepts commands from whatsapp_number in config
  - Format: "SI <service_id>" (case-insensitive) or "NO"
  - Up to MAX_RESTORE_RETRIES retries with exponential backoff on failure
  - Timeout: 5 minutes to confirm, 10 minutes for the restore engine
  - For mayor/packing: Devin also verifies SAP DIAPI middleware
"""
import asyncio
import logging
import httpx
import shutil
from datetime import datetime
from typing import Dict, Optional
from pathlib import Path

from app.models.restore import PendingRestore, RestoreStatus
from app.services import db_service
from app.services.config_service import load_config
from app.services.health_service import check_service

logger = logging.getLogger("sofia.restore")

# In-memory store of pending/active restores keyed by service_id
_pending: Dict[str, PendingRestore] = {}

# Confirmation timeout: user must reply within this many minutes
CONFIRM_TIMEOUT_MINUTES = 5

# How long to wait for Devin / PS1 to finish
DEVIN_TIMEOUT_SECONDS = 600   # 10 minutes
PS1_TIMEOUT_SECONDS = 300

# Retry policy
MAX_RESTORE_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 30

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


# ── helpers ─────────────────────────────────────────────────────────────────


def _ps1_script_path(service_id: str) -> Path:
    return Path(__file__).parent.parent / "scripts" / f"restore_{service_id}.ps1"


async def _persist_new_restore(restore: PendingRestore) -> None:
    """Insert a new row in the restores table and store its id on the in-memory object."""
    try:
        restore.db_id = await db_service.save_restore({
            "service_id": restore.service_id,
            "service_name": restore.service_name,
            "status": restore.status.value,
            "trigger_mode": restore.trigger_mode,
            "requested_at": restore.requested_at.isoformat() if restore.requested_at else None,
            "confirmed_at": restore.confirmed_at.isoformat() if restore.confirmed_at else None,
            "finished_at": restore.finished_at.isoformat() if restore.finished_at else None,
            "result_message": restore.result_message,
            "devin_output": restore.devin_output,
            "retry_count": restore.retry_count,
            "restore_method": restore.restore_method,
        })
    except Exception as exc:
        logger.warning(f"[RESTORE] save_restore failed: {exc}")


async def _persist_update(restore: PendingRestore) -> None:
    if not restore.db_id:
        return
    fields = {
        "status": restore.status.value,
        "confirmed_at": restore.confirmed_at.isoformat() if restore.confirmed_at else None,
        "finished_at": restore.finished_at.isoformat() if restore.finished_at else None,
        "result_message": restore.result_message,
        "devin_output": restore.devin_output,
        "retry_count": restore.retry_count,
        "restore_method": restore.restore_method,
        "trigger_mode": restore.trigger_mode,
    }
    try:
        await db_service.update_restore(restore.db_id, **fields)
    except Exception as exc:
        logger.warning(f"[RESTORE] update_restore failed: {exc}")


# ── public API ──────────────────────────────────────────────────────────────


async def notify_down_with_restore_prompt(service_id: str, service_name: str) -> None:
    """
    Called by health_service when a service crosses the failure threshold.
    - If the service has auto_restore=True, restart it immediately.
    - Otherwise send a WhatsApp prompt asking the user for confirmation.
    """
    cfg = load_config()
    svc_cfg = next((s for s in cfg.services if s.id == service_id), None)

    if svc_cfg is None or not svc_cfg.restore_enabled:
        # Service not restoreable — send a plain notification only
        text = (
            f"🔴 *{service_name}* ha caído.\n"
            f"Este servicio no tiene restauración automática configurada.\n"
            f"Revisar manualmente."
        )
        await _send_simple_message(text)
        return

    # Don't spam: if there's already a pending/running restore, skip
    existing = _pending.get(service_id)
    if existing and existing.status in (RestoreStatus.PENDING, RestoreStatus.RUNNING, RestoreStatus.CONFIRMED):
        logger.info(f"[RESTORE] Already a pending restore for {service_id}, skipping prompt.")
        return

    if svc_cfg.auto_restore:
        # Auto mode — skip the WhatsApp confirmation dance
        await _send_simple_message(f"🤖 Auto-restaurando *{service_name}*...")
        restore = PendingRestore(
            service_id=service_id,
            service_name=service_name,
            status=RestoreStatus.CONFIRMED,
            requested_at=datetime.utcnow(),
            confirmed_at=datetime.utcnow(),
            trigger_mode="auto",
        )
        _pending[service_id] = restore
        await _persist_new_restore(restore)
        asyncio.create_task(_run_restore(service_id))
        return

    # Manual mode — ask the user
    text = (
        f"🔴 *{service_name}* ha caído.\n\n"
        f"Responde *SI {service_id.upper()}* para que Devin lo restaure automáticamente,\n"
        f"o *NO* para ignorar.\n\n"
        f"_(Tienes {CONFIRM_TIMEOUT_MINUTES} minutos para responder)_"
    )
    await _send_simple_message(text)
    restore = PendingRestore(
        service_id=service_id,
        service_name=service_name,
        status=RestoreStatus.PENDING,
        requested_at=datetime.utcnow(),
        trigger_mode="manual",
    )
    _pending[service_id] = restore
    await _persist_new_restore(restore)
    asyncio.create_task(_expire_pending(service_id))


async def trigger_manual_restore(service_id: str) -> PendingRestore:
    """
    Trigger a restore from the UI/API. Skips WhatsApp confirmation and runs
    the restore engine immediately. Raises ValueError on bad input.
    """
    cfg = load_config()
    svc_cfg = next((s for s in cfg.services if s.id == service_id), None)
    if svc_cfg is None:
        raise ValueError(f"Service '{service_id}' not found")
    if not svc_cfg.restore_enabled:
        raise ValueError(f"Service '{service_id}' does not have restore_enabled")

    existing = _pending.get(service_id)
    if existing and existing.status in (RestoreStatus.PENDING, RestoreStatus.RUNNING, RestoreStatus.CONFIRMED):
        return existing

    restore = PendingRestore(
        service_id=service_id,
        service_name=svc_cfg.name,
        status=RestoreStatus.CONFIRMED,
        requested_at=datetime.utcnow(),
        confirmed_at=datetime.utcnow(),
        trigger_mode="manual",
    )
    _pending[service_id] = restore
    await _persist_new_restore(restore)
    asyncio.create_task(_run_restore(service_id))
    return restore


async def _expire_pending(service_id: str) -> None:
    """Mark pending restore as expired if user doesn't confirm in time."""
    await asyncio.sleep(CONFIRM_TIMEOUT_MINUTES * 60)
    restore = _pending.get(service_id)
    if restore and restore.status == RestoreStatus.PENDING:
        restore.status = RestoreStatus.EXPIRED
        restore.finished_at = datetime.utcnow()
        restore.result_message = "Sin respuesta del usuario."
        logger.info(f"[RESTORE] Restore for {service_id} expired (no confirmation).")
        await _persist_update(restore)
        await _send_simple_message(f"⏰ Sin respuesta — restauración de *{restore.service_name}* cancelada.")
        # Optional escalation to backup numbers
        cfg = load_config()
        if cfg.alerts.escalation_enabled and cfg.alerts.escalation_numbers:
            from app.services import whatsapp_service
            await whatsapp_service.send_to_escalation(
                cfg.alerts,
                f"⚠️ *{restore.service_name}* sigue caído y no hubo respuesta para restaurar.",
            )


async def handle_incoming_message(sender_phone: str, text: str) -> None:
    """
    Called by the webhook router when a WhatsApp message is received.
    Validates sender, parses command, and triggers restore if appropriate.
    """
    cfg = load_config()
    authorized = cfg.alerts.whatsapp_number.replace("+", "").replace("@c.us", "")
    sender_normalized = sender_phone.replace("+", "").replace("@c.us", "").replace("@lid", "")

    if sender_normalized != authorized:
        logger.debug(f"[RESTORE] Ignoring message from unauthorized sender: {sender_phone}")
        return

    text = text.strip().upper()
    logger.info(f"[RESTORE] Received command from authorized sender: '{text}'")

    if text == "NO":
        cancelled = []
        for sid, restore in list(_pending.items()):
            if restore.status == RestoreStatus.PENDING:
                restore.status = RestoreStatus.REJECTED
                restore.finished_at = datetime.utcnow()
                restore.result_message = "Cancelado por el usuario."
                await _persist_update(restore)
                cancelled.append(restore.service_name)
        if cancelled:
            await _send_simple_message(f"✅ Restauración cancelada para: {', '.join(cancelled)}")
        return

    if text.startswith("SI "):
        service_id = text[3:].strip().lower()
        restore = _pending.get(service_id)

        if not restore:
            await _send_simple_message(f"⚠️ No hay restauración pendiente para *{service_id}*.")
            return
        if restore.status != RestoreStatus.PENDING:
            await _send_simple_message(f"⚠️ La restauración de *{restore.service_name}* ya está en estado: {restore.status.value}")
            return

        restore.status = RestoreStatus.CONFIRMED
        restore.confirmed_at = datetime.utcnow()
        await _persist_update(restore)
        logger.info(f"[RESTORE] User confirmed restore for {service_id}")
        asyncio.create_task(_run_restore(service_id))
        return

    logger.debug(f"[RESTORE] Ignoring unrecognized command: '{text}'")


def _build_devin_prompt(service_id: str, service_name: str) -> str:
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


async def _run_devin(service_id: str, service_name: str) -> tuple[bool, str]:
    """
    Run a Devin session. Returns (success, output_tail).
    Returns (False, "<reason>") if Devin CLI not installed.
    """
    devin_bin = shutil.which("devin") or shutil.which("devin.exe")
    if not devin_bin:
        return False, "DEVIN_NOT_FOUND"

    prompt = _build_devin_prompt(service_id, service_name)
    cwd = SERVICE_CONTEXT.get(service_id, {}).get("work_dir", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            devin_bin, "--print", "--permission-mode", "dangerous", "--", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd if cwd and Path(cwd).exists() else None,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=DEVIN_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return False, f"DEVIN timeout > {DEVIN_TIMEOUT_SECONDS}s"
        output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        tail = output[-2000:] if len(output) > 2000 else output
        success = proc.returncode == 0 and "RESTORE_FAILED" not in (output or "")
        return success, tail
    except Exception as exc:
        return False, f"DEVIN error: {exc}"


async def _run_ps1(service_id: str) -> tuple[bool, str]:
    """
    Run the PowerShell fallback script for this service.
    Returns (success, output_tail).
    """
    script = _ps1_script_path(service_id)
    if not script.exists():
        return False, "PS1_NOT_FOUND"
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-ExecutionPolicy", "Bypass", "-File", str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=PS1_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return False, f"PS1 timeout > {PS1_TIMEOUT_SECONDS}s"
        output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        tail = output[-2000:] if len(output) > 2000 else output
        return proc.returncode == 0, tail
    except FileNotFoundError:
        # PowerShell not installed (e.g. Linux)
        return False, "POWERSHELL_NOT_INSTALLED"
    except Exception as exc:
        return False, f"PS1 error: {exc}"


async def _run_restore(service_id: str) -> None:
    """Run the restore engine — Devin first, PS1 fallback, retry with backoff."""
    restore = _pending.get(service_id)
    if not restore:
        return

    restore.status = RestoreStatus.RUNNING
    await _persist_update(restore)

    # Pick engine: prefer Devin, fall back to PS1 if Devin not available
    devin_available = shutil.which("devin") is not None or shutil.which("devin.exe") is not None
    ps1_available = _ps1_script_path(service_id).exists()

    if devin_available:
        engine = "devin"
    elif ps1_available:
        engine = "ps1_script"
    else:
        engine = None

    restore.restore_method = engine

    if engine is None:
        restore.status = RestoreStatus.FAILED
        restore.finished_at = datetime.utcnow()
        restore.result_message = "Devin CLI no encontrado y no hay script PS1 de fallback."
        await _persist_update(restore)
        await _send_simple_message(
            f"❌ No hay método de restauración disponible para *{restore.service_name}*. "
            f"Instalar Devin CLI o crear restore_{service_id}.ps1."
        )
        return

    method_msg = "Devin" if engine == "devin" else "script PowerShell"
    await _send_simple_message(
        f"🤖 Restaurando *{restore.service_name}* usando {method_msg}...\n"
        f"_Puede tomar unos minutos. Te avisaré cuando termine._"
    )
    logger.info(f"[RESTORE] Launching {engine} for {service_id}")

    success, output_tail = (False, "")
    if engine == "devin":
        success, output_tail = await _run_devin(service_id, restore.service_name)
        if not success and output_tail == "DEVIN_NOT_FOUND" and ps1_available:
            engine = "ps1_script"
            restore.restore_method = engine
            success, output_tail = await _run_ps1(service_id)
    else:
        success, output_tail = await _run_ps1(service_id)

    restore.devin_output = output_tail

    # Independent health check to confirm
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
    elapsed = int((restore.finished_at - (restore.confirmed_at or restore.requested_at)).total_seconds())

    summary_line = None
    if output_tail:
        for line in reversed(output_tail.splitlines()):
            if line.startswith("RESTORE_SUCCESS:") or line.startswith("RESTORE_FAILED:"):
                summary_line = line
                break

    if came_up and diapi_ok:
        restore.status = RestoreStatus.SUCCESS
        summary = (
            summary_line.replace("RESTORE_SUCCESS:", "").strip()
            if summary_line and "SUCCESS" in summary_line
            else "Servicio restaurado."
        )
        restore.result_message = summary
        msg = (
            f"✅ *{restore.service_name}* restaurado en {elapsed}s ({method_msg}).\n_{summary}_"
        )
        if service_id in DIAPI_SERVICES:
            msg += "\n✅ Middleware SAP DIAPI respondiendo."
        await _persist_update(restore)
        await _send_simple_message(msg)
        return

    if came_up and not diapi_ok:
        restore.status = RestoreStatus.FAILED
        restore.result_message = "Servicio levantó pero DIAPI no responde."
        await _persist_update(restore)
        await _send_simple_message(
            f"⚠️ *{restore.service_name}* levantó pero el middleware SAP DIAPI no responde.\n"
            f"Revisar manualmente."
        )
        return

    # Failed — consider retrying
    if restore.retry_count < MAX_RESTORE_RETRIES:
        next_retry = restore.retry_count + 1
        backoff = RETRY_BACKOFF_BASE_SECONDS * (2 ** restore.retry_count)
        restore.retry_count = next_retry
        await _persist_update(restore)
        await _send_simple_message(
            f"🔄 Reintento {next_retry}/{MAX_RESTORE_RETRIES} para *{restore.service_name}* "
            f"en {backoff}s..."
        )
        logger.info(f"[RESTORE] Retry {next_retry}/{MAX_RESTORE_RETRIES} for {service_id} in {backoff}s")
        await asyncio.sleep(backoff)
        restore.status = RestoreStatus.CONFIRMED  # so _run_restore proceeds
        await _persist_update(restore)
        await _run_restore(service_id)
        return

    # Out of retries — definitive failure
    restore.status = RestoreStatus.FAILED
    summary = (
        summary_line.replace("RESTORE_FAILED:", "").strip()
        if summary_line and "FAILED" in summary_line
        else "El servicio no respondió al health check."
    )
    restore.result_message = summary
    await _persist_update(restore)
    await _send_simple_message(
        f"❌ *{restore.service_name}* no levantó tras {elapsed}s y {MAX_RESTORE_RETRIES} reintentos.\n"
        f"_{summary}_\n"
        f"Revisar logs manualmente."
    )
    logger.error(f"[RESTORE] {service_id} failed after retries. Summary: {summary}")


async def _check_diapi() -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(DIAPI_HEALTH_URL)
            return resp.status_code < 500
    except Exception:
        return False


async def _send_simple_message(text: str) -> None:
    """Send a plain WhatsApp message via whatsapp_service (queues on failure)."""
    cfg = load_config()
    if not cfg.alerts.whatsapp_enabled:
        return
    from app.services import whatsapp_service
    await whatsapp_service.send_message(cfg.alerts, text)
