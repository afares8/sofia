"""
Active SDK ingestion endpoint.
Apps call POST /ingest/event to push errors directly to Sofia.
Errors are grouped by fingerprint — same error = increment count, not duplicate row.
"""
import json
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

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
    breadcrumbs: Optional[List[dict]] = None
    tags: Optional[Any] = None       # dict or already-stringified JSON
    environment: Optional[str] = None
    release: Optional[str] = None


@router.post("/event")
async def ingest_event(payload: IngestPayload):
    cfg = load_config()

    # Normalize tags into a JSON string (DB column is TEXT)
    tags_str: Optional[str] = None
    if payload.tags is not None:
        if isinstance(payload.tags, str):
            tags_str = payload.tags[:2000]
        else:
            try:
                tags_str = json.dumps(payload.tags)[:2000]
            except Exception:
                tags_str = None

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
        tags=tags_str,
        environment=payload.environment,
        release=payload.release,
    )

    issue_id, is_new, is_regression = await db_service.upsert_event(event)

    # Attach breadcrumbs to the latest occurrence
    if payload.breadcrumbs:
        try:
            await db_service.insert_occurrence_breadcrumbs(
                issue_id, json.dumps(payload.breadcrumbs)[:8000],
            )
        except Exception:
            pass

    # Alert on new issues or regressions
    should_alert = (is_new or is_regression) and event.level in ("ERROR", "CRITICAL")
    if should_alert:
        prefix = "🔄 REGRESIÓN: " if is_regression else ""
        sent = await whatsapp_service.send_alert(
            cfg.alerts, event.service_name, event.service_id,
            event.level, prefix + event.message, event.detail or "",
        )
        if sent:
            await db_service.mark_notified(issue_id)

    return {
        "ok": True,
        "issue_id": issue_id,
        "is_new": is_new,
        "is_regression": is_regression,
    }
