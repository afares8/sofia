"""Health check endpoints."""
from fastapi import APIRouter
from app.services.health_service import get_all_statuses, check_service
from app.services.config_service import load_config
from app.models.event import ServiceStatus
from typing import List

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/", response_model=List[ServiceStatus])
async def list_statuses():
    """Return cached status for all services."""
    return list(get_all_statuses().values())


@router.post("/check/{service_id}", response_model=ServiceStatus)
async def force_check(service_id: str):
    """Immediately re-check a specific service."""
    cfg = load_config()
    svc = next((s for s in cfg.services if s.id == service_id), None)
    if not svc:
        from fastapi import HTTPException
        raise HTTPException(404, f"Service '{service_id}' not found")
    return await check_service(svc)
