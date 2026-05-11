"""
Restore status router - exposes pending/active restores for the UI.
"""
from fastapi import APIRouter
from app.services.restore_service import get_all_pending

router = APIRouter(prefix="/restore", tags=["restore"])


@router.get("/")
async def list_restores():
    """Return all pending/active/finished restores."""
    pending = get_all_pending()
    return [
        {
            "service_id": r.service_id,
            "service_name": r.service_name,
            "status": r.status.value,
            "requested_at": r.requested_at.isoformat() if r.requested_at else None,
            "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "result_message": r.result_message,
            "devin_output": r.devin_output,
        }
        for r in pending.values()
    ]
