"""
Config models - all settings editable from UI and persisted to JSON.
"""
from pydantic import BaseModel
from typing import List, Optional


class ServiceConfig(BaseModel):
    id: str
    name: str
    url: str                  # health-check URL  e.g. http://localhost:8075/health
    enabled: bool = True
    log_path: Optional[str] = None   # absolute path to log file (passive monitoring)
    expected_status: int = 200
    timeout_seconds: int = 5


class AlertConfig(BaseModel):
    whatsapp_enabled: bool = True
    whatsapp_number: str = "50766662916"   # default: your number
    wppconnect_url: str = "http://localhost:21465"
    wppconnect_token: str = "THISISMYSECURETOKEN"
    wppconnect_session: str = "default"
    cooldown_minutes: int = 10           # don't spam same alert twice


class MonitorConfig(BaseModel):
    poll_interval_seconds: int = 30
    log_tail_lines: int = 200
    error_retention_days: int = 7
    services: List[ServiceConfig] = []
    alerts: AlertConfig = AlertConfig()


DEFAULT_SERVICES: List[ServiceConfig] = [
    ServiceConfig(
        id="mayor",
        name="Mayor",
        url="http://192.168.0.123:8075/health",
        log_path="D:/mayor/backend/logs/app.log",
        enabled=True,
    ),
    ServiceConfig(
        id="packing",
        name="Packing",
        url="http://192.168.0.123:8100/health",
        log_path="D:/packing/backend/logs/app.log",
        enabled=True,
    ),
    ServiceConfig(
        id="pantalla",
        name="Pantalla",
        url="http://192.168.0.123:8000/health",
        log_path="D:/Pantalla/backend/logs/app.log",
        enabled=True,
    ),
    ServiceConfig(
        id="cortana",
        name="Cortana (WhatsApp AI)",
        url="http://192.168.0.123:8200/health",
        log_path="D:/Cortana/backend/logs/app.log",
        enabled=True,
    ),
    ServiceConfig(
        id="wppconnect",
        name="WppConnect",
        url="http://192.168.0.123:21465/api/default/status-session",
        enabled=True,
    ),
]
