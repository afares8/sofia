"""
Active SDK ingestion endpoint.
Apps call POST /ingest/event to push errors directly to Sofia.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.models.event import ErrorEvent
from app.services import db_service, whatsapp_service
from app.services.config_service import load_config

router = APIRouter(prefix="/ingest", tags=["ingest"])


class IngestPayload(BaseModel):
    service_id: str
    service_name: str
    level: str                   # ERROR | WARNING | CRITICAL | INFO
    message: str
    detail: Optional[str] = None
    traceback: Optional[str] = None
    timestamp: Optional[datetime] = None


@router.post("/event")
async def ingest_event(payload: IngestPayload):
    """Receive an error event pushed by a service SDK."""
    cfg = load_config()

    # Validate service_id
    known_ids = {s.id for s in cfg.services}
    if payload.service_id not in known_ids:
        # Accept anyway but flag as unknown
        pass

    event = ErrorEvent(
        service_id=payload.service_id,
        service_name=payload.service_name,
        level=payload.level.upper(),
        message=payload.message,
        detail=payload.detail,
        traceback=payload.traceback,
        source="active",
        timestamp=payload.timestamp or datetime.utcnow(),
    )
    event_id = await db_service.insert_event(event)

    # Alert on ERROR or CRITICAL
    if event.level in ("ERROR", "CRITICAL"):
        sent = await whatsapp_service.send_alert(
            cfg.alerts,
            event.service_name,
            event.service_id,
            event.level,
            event.message,
            event.detail or "",
        )
        if sent:
            await db_service.mark_notified(event_id)

    return {"ok": True, "event_id": event_id}
