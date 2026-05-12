"""
Restore status router - exposes pending/active restores, history, and a manual
trigger endpoint for the UI.
"""
from fastapi import APIRouter, HTTPException, Query

from app.services import db_service, restore_service

router = APIRouter(prefix="/restore", tags=["restore"])


def _pending_to_dict(r) -> dict:
    return {
        "service_id": r.service_id,
        "service_name": r.service_name,
        "status": r.status.value,
        "requested_at": r.requested_at.isoformat() if r.requested_at else None,
        "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "result_message": r.result_message,
        "devin_output": r.devin_output,
        "retry_count": r.retry_count,
        "trigger_mode": r.trigger_mode,
        "restore_method": r.restore_method,
    }


@router.get("/")
async def list_restores(limit: int = Query(50, ge=1, le=500)):
    """
    Return active in-memory restores combined with the latest history rows
    from the DB. Active entries come first, then the most recent finished ones.
    """
    pending = list(restore_service.get_all_pending().values())
    active_ids = {p.service_id for p in pending if p.status.value in {"pending", "confirmed", "running"}}

    history_rows = await db_service.get_restore_history(limit=limit)
    out = [_pending_to_dict(p) for p in pending]
    for row in history_rows:
        # Skip rows that correspond to an active in-memory restore.
        if row["service_id"] in active_ids and row["status"] in {"pending", "confirmed", "running"}:
            continue
        out.append({
            "service_id": row["service_id"],
            "service_name": row["service_name"],
            "status": row["status"],
            "requested_at": row["requested_at"],
            "confirmed_at": row["confirmed_at"],
            "finished_at": row["finished_at"],
            "result_message": row["result_message"],
            "devin_output": row["devin_output"],
            "retry_count": row.get("retry_count", 0),
            "trigger_mode": row.get("trigger_mode", "manual"),
            "restore_method": row.get("restore_method"),
        })
    return out


@router.get("/history")
async def restore_history(
    limit: int = Query(50, ge=1, le=500),
    service_id: str | None = None,
):
    """Return persisted restore history from the DB."""
    return await db_service.get_restore_history(limit=limit, service_id=service_id)


@router.post("/trigger/{service_id}")
async def trigger_restore(service_id: str):
    """Trigger a restore from the UI bypassing the WhatsApp confirmation flow."""
    try:
        restore = await restore_service.trigger_manual_restore(service_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _pending_to_dict(restore)
