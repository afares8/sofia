"""
Email fallback alert service.

When WPPConnect (WhatsApp) is unreachable, Sofia cannot notify the team via
its primary alert channel. This service provides a secondary, independent
channel via SMTP email so that critical alerts (e.g. WPPConnect down, service
unreachable) are never silently lost.

SMTP configuration is read from environment variables, mirroring the ones
used by the Mayor backend (MAIL_HOST, MAIL_USERNAME, ...) so the same Gmail
app-password credentials work for both apps without duplication.

The recipients are configurable via SOFIA_ALERT_EMAIL (comma-separated,
default: ahmed@magnate-inc.com,order@magnate-inc.com).
"""
import asyncio
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import Callable, Dict, Optional

logger = logging.getLogger("sofia.email_alert")

SMTP_HOST = os.environ.get("SMTP_HOST") or os.environ.get("MAIL_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT") or os.environ.get("MAIL_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER") or os.environ.get("MAIL_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD") or os.environ.get("MAIL_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL") or os.environ.get("MAIL_FROM", SMTP_USER)
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME") or os.environ.get("MAIL_FROM_NAME", "Sofia Monitor")

# Default recipients for Sofia fallback alerts (comma-separated; smtplib's
# send_message resolves every address listed in the To header).
ALERT_EMAIL_RECIPIENT = os.environ.get(
    "SOFIA_ALERT_EMAIL", "ahmed@magnate-inc.com,order@magnate-inc.com"
)

# Cooldown to avoid email flooding when WPPConnect is down for a long time.
# Stored in-memory; resets on restart (acceptable for a fallback channel).
_last_email_sent: Optional[datetime] = None
_EMAIL_COOLDOWN_SECONDS = 300  # 5 minutes

# Correos diferidos pendientes (clave -> task). Permiten esperar un tiempo
# antes de mandar el aviso: si al vencer el servicio ya se recupero (p. ej.
# WPPConnect reiniciado por su watchdog), el correo no se envia.
_deferred_tasks: Dict[str, "asyncio.Task"] = {}


def is_configured() -> bool:
    """Return True if SMTP credentials are available."""
    return bool(SMTP_USER and SMTP_PASSWORD)


def _is_on_cooldown() -> bool:
    global _last_email_sent
    if _last_email_sent is None:
        return False
    return (datetime.utcnow() - _last_email_sent).total_seconds() < _EMAIL_COOLDOWN_SECONDS


def send_fallback_email(
    subject: str,
    body: str,
    recipient: Optional[str] = None,
) -> bool:
    """
    Send a fallback alert email via SMTP.

    Returns True on success, False on failure (or if on cooldown / not configured).
    Failures are logged but never raise — this is a best-effort secondary channel.
    """
    global _last_email_sent

    if not is_configured():
        logger.warning("[EMAIL] SMTP not configured (SMTP_USER/SMTP_PASSWORD missing), skipping fallback email.")
        return False

    if _is_on_cooldown():
        logger.debug("[EMAIL] Fallback email on cooldown, skipping.")
        return False

    to_addr = recipient or ALERT_EMAIL_RECIPIENT
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    msg["To"] = to_addr
    msg["Subject"] = subject

    html = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
      <h2 style="color: #c0392b;">⚠️ Sofia Monitor — Alerta de Respaldo</h2>
      <p>El canal principal de alertas (WhatsApp/WPPConnect) no está disponible.
         Este es un aviso de respaldo por correo.</p>
      <hr style="border: 1px solid #eee;" />
      <pre style="white-space: pre-wrap; font-family: Arial, sans-serif;">{body}</pre>
      <hr style="border: 1px solid #eee;" />
      <p style="color: #999; font-size: 12px;">
        Enviado por Sofia Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
      </p>
    </body></html>
    """
    plain = f"Sofia Monitor — Alerta de Respaldo\n\n{body}\n\n---\nEnviado: {datetime.now()}"

    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        _last_email_sent = datetime.utcnow()
        logger.info("[EMAIL] Fallback alert email sent to %s: %s", to_addr, subject)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("[EMAIL] SMTP authentication failed — check SMTP_USER/SMTP_PASSWORD.")
    except smtplib.SMTPException as exc:
        logger.error("[EMAIL] SMTP error sending fallback email: %s", exc)
    except Exception as exc:
        logger.error("[EMAIL] Unexpected error sending fallback email: %s", exc)
    return False


def schedule_fallback_email(
    subject: str,
    body: str,
    delay_seconds: float,
    cancel_check: Optional[Callable[[], bool]] = None,
    key: Optional[str] = None,
) -> bool:
    """Programa un correo de respaldo para dentro de `delay_seconds`.

    Pensado para caidas transitorias: si al vencer el plazo `cancel_check()`
    devuelve True (p. ej. el servicio ya se recupero y solo era un reinicio),
    el correo NO se envia. Un schedule nuevo con la misma clave reemplaza al
    anterior. Sin event loop disponible cae al envio inmediato (no se pierde).
    """
    if not is_configured():
        logger.warning("[EMAIL] SMTP not configured, no se programa el correo diferido.")
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return send_fallback_email(subject, body)
    key = key or subject
    prev = _deferred_tasks.get(key)
    if prev is not None and not prev.done():
        prev.cancel()
    task = loop.create_task(
        _send_deferred(subject, body, float(delay_seconds), cancel_check))
    _deferred_tasks[key] = task

    def _cleanup(t: "asyncio.Task", k: str = key) -> None:
        if _deferred_tasks.get(k) is t:
            _deferred_tasks.pop(k, None)

    task.add_done_callback(_cleanup)
    logger.info("[EMAIL] Correo diferido programado en %.0fs: %s",
                delay_seconds, subject)
    return True


async def _send_deferred(
    subject: str,
    body: str,
    delay_seconds: float,
    cancel_check: Optional[Callable[[], bool]],
) -> None:
    try:
        await asyncio.sleep(delay_seconds)
    except asyncio.CancelledError:
        logger.info("[EMAIL] Correo diferido cancelado: %s", subject)
        return
    if cancel_check is not None:
        try:
            if cancel_check():
                logger.info("[EMAIL] Correo diferido descartado (ya no aplica): %s",
                            subject)
                return
        except Exception as exc:
            logger.warning("[EMAIL] cancel_check fallo (%s); se envia igual", exc)
    send_fallback_email(subject, body)
