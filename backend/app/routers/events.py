"""Issues (grouped errors) and occurrences endpoints."""
from fastapi import APIRouter, Query
from typing import Optional
from app.services import db_service

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/")
async def list_issues(
    service_id: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    resolved: bool = Query(False),
    limit: int = Query(200, le=1000),
    since_hours: int = Query(24 * 7, le=720),
):
    return await db_service.get_issues(service_id, level, resolved, limit, since_hours)


@router.get("/{issue_id}/occurrences")
async def list_occurrences(issue_id: int, limit: int = Query(50, le=200)):
    return await db_service.get_occurrences(issue_id, limit)


@router.post("/{issue_id}/resolve")
async def resolve_issue(issue_id: int):
    await db_service.resolve_issue(issue_id)
    return {"ok": True}


@router.delete("/purge")
async def purge_events(retention_days: int = Query(7)):
    await db_service.purge_old_events(retention_days)
    return {"ok": True, "message": f"Eventos anteriores a {retention_days} días eliminados."}
