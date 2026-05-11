"""
Config CRUD - all settings editable from the UI.
"""
from fastapi import APIRouter
from app.models.config import MonitorConfig, ServiceConfig, AlertConfig
from app.services.config_service import load_config, save_config
from typing import List

router = APIRouter(prefix="/config", tags=["config"])


@router.get("/", response_model=MonitorConfig)
async def get_config():
    return load_config()


@router.put("/", response_model=MonitorConfig)
async def update_config(new_cfg: MonitorConfig):
    save_config(new_cfg)
    return new_cfg


# --- Services ---

@router.get("/services", response_model=List[ServiceConfig])
async def list_services():
    return load_config().services


@router.post("/services", response_model=ServiceConfig)
async def add_service(svc: ServiceConfig):
    cfg = load_config()
    if any(s.id == svc.id for s in cfg.services):
        from fastapi import HTTPException
        raise HTTPException(400, f"Service id '{svc.id}' already exists")
    cfg.services.append(svc)
    save_config(cfg)
    return svc


@router.put("/services/{service_id}", response_model=ServiceConfig)
async def update_service(service_id: str, updated: ServiceConfig):
    cfg = load_config()
    for i, s in enumerate(cfg.services):
        if s.id == service_id:
            cfg.services[i] = updated
            save_config(cfg)
            return updated
    from fastapi import HTTPException
    raise HTTPException(404, f"Service '{service_id}' not found")


@router.delete("/services/{service_id}")
async def delete_service(service_id: str):
    cfg = load_config()
    original_len = len(cfg.services)
    cfg.services = [s for s in cfg.services if s.id != service_id]
    if len(cfg.services) == original_len:
        from fastapi import HTTPException
        raise HTTPException(404, f"Service '{service_id}' not found")
    save_config(cfg)
    return {"ok": True}


# --- Alert config ---

@router.get("/alerts", response_model=AlertConfig)
async def get_alert_config():
    return load_config().alerts


@router.put("/alerts", response_model=AlertConfig)
async def update_alert_config(alerts: AlertConfig):
    cfg = load_config()
    cfg.alerts = alerts
    save_config(cfg)
    return alerts


# --- Test alert ---

@router.post("/alerts/test")
async def test_alert():
    from app.services.whatsapp_service import send_alert
    from app.services.config_service import load_config as lc
    cfg = lc()
    # Temporarily bypass cooldown by using unique key
    from app.services import whatsapp_service as wa
    wa._cooldown.clear()
    sent = await send_alert(
        cfg.alerts, "Sofia Monitor", "sofia", "INFO",
        "Prueba de alerta exitosa ✅", "Si recibiste esto, WhatsApp está configurado correctamente."
    )
    return {"sent": sent}
