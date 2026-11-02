"""Escalado del puente Sofia-Devin (WhatsApp + correo).

El puente es un proceso aparte (solo stdlib, no importa `app.*`). Cuando un
lote de avisos lleva varios reintentos fallidos y el CLI sigue sin entregar,
llama a este endpoint para que Sofia use sus canales normales de alerta.

Se autentica con un token de archivo local (no viaja en la URL ni en el repo):
`SOFIA_BRIDGE_TOKEN_FILE` o `C:/ProgramData/ServerSofia/bridge-alert.token`.
Sin token configurado el endpoint falla cerrado.
"""
import asyncio
import hmac
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.services import email_alert_service, whatsapp_service
from app.services.config_service import load_config

router = APIRouter(prefix="/bridge", tags=["bridge"])

TOKEN_FILE_DEFAULT = r"C:\ProgramData\ServerSofia\bridge-alert.token"


def _expected_token() -> str:
    path = os.environ.get("SOFIA_BRIDGE_TOKEN_FILE", TOKEN_FILE_DEFAULT)
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _whatsapp_cfg(alerts):
    """Permite el canal WhatsApp solo para el escalado del puente."""
    if not getattr(alerts, "whatsapp_enabled", False) and getattr(
            alerts, "bridge_escalation_whatsapp", False):
        try:
            return alerts.model_copy(update={"whatsapp_enabled": True})
        except AttributeError:
            return alerts
    return alerts


class EscalationPayload(BaseModel):
    group: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    status: Optional[str] = None
    attempts: int = 0
    escalation: int = 0
    error: Optional[str] = None
    pending: int = 0
    cursor: int = 0
    dropped: int = 0
    retry_interval_minutes: int = 10


@router.post("/escalation")
async def bridge_escalation(payload: EscalationPayload,
                            x_bridge_token: str = Header(default="")):
    expected = _expected_token()
    if not expected:
        raise HTTPException(503, "token del puente no configurado")
    if not hmac.compare_digest(x_bridge_token, expected):
        raise HTTPException(403, "token del puente invalido")

    cfg = load_config()
    name = f"Sofia-Devin ({payload.group})"
    title = (f"{name} no entrega avisos al CLI "
             f"({payload.attempts} intento(s))")
    detail = "\n".join([
        f"Grupo: {payload.group}",
        f"Estado: {payload.status or 'n/d'}",
        f"Reintentos fallidos: {payload.attempts} "
        f"(cada {payload.retry_interval_minutes} min)",
        f"Ultimo error: {(payload.error or 'n/d')[:500]}",
        f"Cola: {payload.pending} aviso(s), cursor {payload.cursor}, "
        f"desalojados {payload.dropped}",
        "Los avisos siguen guardados en Sofia; no se perdieron.",
    ])
    wa_ok = await whatsapp_service.send_alert(
        _whatsapp_cfg(cfg.alerts), name,
        f"sofia-devin-{payload.group}-escalation",
        "CRITICAL", title, detail)
    email_ok = await asyncio.to_thread(
        email_alert_service.send_fallback_email,
        f"[Sofia] {title}", detail)
    return {"ok": True, "whatsapp": bool(wa_ok), "email": bool(email_ok)}
