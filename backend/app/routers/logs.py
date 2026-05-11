"""Log tail endpoint for UI live log viewer."""
from fastapi import APIRouter, Query, HTTPException
from typing import List
from app.services.log_service import read_log_tail
from app.services.config_service import load_config

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("/{service_id}", response_model=List[str])
async def get_log_tail(service_id: str, lines: int = Query(100, le=500)):
    cfg = load_config()
    svc = next((s for s in cfg.services if s.id == service_id), None)
    if not svc:
        raise HTTPException(404, f"Service '{service_id}' not found")
    if not svc.log_path:
        return [f"No log_path configured for service '{service_id}'"]
    return await read_log_tail(svc.log_path, lines)
