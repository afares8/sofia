"""
Email fallback alert service.

When WPPConnect (WhatsApp) is unreachable, Sofia cannot notify the team via
its primary alert channel. This service provides a secondary, independent
channel via SMTP email so that critical alerts (e.g. WPPConnect down, service
unreachable) are never silently lost.

SMTP configuration is read from environment variables, mirroring the ones
used by the Mayor backend (MAIL_HOST, MAIL_USERNAME, ...) so the same Gmail
app-password credentials work for both apps without duplication.

The recipient is configurable via SOFIA_ALERT_EMAIL (default:
ahmed@magnate-inc.com).
"""
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import Optional

logger = logging.getLogger("sofia.email_alert")

SMTP_HOST = os.environ.get("SMTP_HOST") or os.environ.get("MAIL_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT") or os.environ.get("MAIL_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER") or os.environ.get("MAIL_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD") or os.environ.get("MAIL_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL") or os.environ.get("MAIL_FROM", SMTP_USER)
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME") or os.environ.get("MAIL_FROM_NAME", "Sofia Monitor")

# Default recipient for Sofia fallback alerts.
ALERT_EMAIL_RECIPIENT = os.environ.get("SOFIA_ALERT_EMAIL", "ahmed@magnate-inc.com")

# Cooldown to avoid email flooding when WPPConnect is down for a long time.
# Stored in-memory; resets on restart (acceptable for a fallback channel).
_last_email_sent: Optional[datetime] = None
_EMAIL_COOLDOWN_SECONDS = 300  # 5 minutes


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
