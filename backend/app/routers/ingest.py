"""
Active SDK ingestion endpoint.
Apps call POST /ingest/event to push errors directly to Sofia.
Errors are grouped by fingerprint — same error = increment count, not duplicate row.
"""
from fastapi import APIRouter
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
    level: str
    message: str
    detail: Optional[str] = None
    traceback: Optional[str] = None
    url: Optional[str] = None
    user_info: Optional[str] = None
    timestamp: Optional[datetime] = None


@router.post("/event")
async def ingest_event(payload: IngestPayload):
    cfg = load_config()

    event = ErrorEvent(
        service_id=payload.service_id,
        service_name=payload.service_name,
        level=payload.level.upper(),
        message=payload.message,
        detail=payload.detail,
        traceback=payload.traceback,
        url=payload.url,
        user_info=payload.user_info,
        source="active",
        timestamp=payload.timestamp or datetime.utcnow(),
    )

    issue_id, is_new = await db_service.upsert_event(event)

    # Only alert on new issues (not every repeated occurrence)
    if is_new and event.level in ("ERROR", "CRITICAL"):
        sent = await whatsapp_service.send_alert(
            cfg.alerts, event.service_name, event.service_id,
            event.level, event.message, event.detail or "",
        )
        if sent:
            await db_service.mark_notified(issue_id)

    return {"ok": True, "issue_id": issue_id, "is_new": is_new}
