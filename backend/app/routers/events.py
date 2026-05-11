"""Error event endpoints."""
from fastapi import APIRouter, Query
from typing import List, Optional
from app.models.event import ErrorEvent
from app.services import db_service

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/", response_model=List[ErrorEvent])
async def list_events(
    service_id: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    limit: int = Query(200, le=1000),
    since_hours: int = Query(24, le=720),
):
    return await db_service.get_events(service_id, level, limit, since_hours)


@router.delete("/purge")
async def purge_events(retention_days: int = Query(7)):
    await db_service.purge_old_events(retention_days)
    return {"ok": True, "message": f"Eventos anteriores a {retention_days} días eliminados."}
