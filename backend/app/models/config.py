"""
Config models - all settings editable from UI and persisted to JSON.
"""
import os
from pydantic import BaseModel
from typing import List, Optional


# Host IP used in default service URLs (can be overridden by env var)
_SOFIA_HOST_IP = os.getenv("SOFIA_HOST_IP", "192.168.0.123")


class ServiceConfig(BaseModel):
    id: str
    name: str
    url: str                  # health-check URL  e.g. http://192.168.0.123:8075/health
    enabled: bool = True
    log_path: Optional[str] = None   # absolute path to log file (passive monitoring)
    expected_status: int = 200
    timeout_seconds: int = 5
    failure_threshold: int = 3       # consecutive failures before alerting (grace for --reload)
    restore_enabled: bool = False    # enable WhatsApp-triggered restore for this service
    auto_restore: bool = False       # if True, auto-restore without asking user via WhatsApp


class AlertConfig(BaseModel):
    whatsapp_enabled: bool = True
    whatsapp_number: str = "50766662916"   # default: your number
    wppconnect_url: str = "http://localhost:21465"
    wppconnect_token: str = "THISISMYSECURETOKEN"
    wppconnect_session: str = "default"
    cooldown_minutes: int = 10           # don't spam same alert twice

    # Multi-channel escalation: if a pending restore expires with no response,
    # forward the alert to additional numbers in order.
    escalation_enabled: bool = False
    escalation_minutes: int = 15
    escalation_numbers: List[str] = []


class AlertRule(BaseModel):
    """
    A dynamic alerting rule evaluated by the rules engine.

    condition_type values:
      - "error_count"      → threshold = max errors in window_minutes
      - "response_ms"      → threshold = max response time in ms
      - "downtime_minutes" → threshold = max downtime minutes
      - "spike"            → threshold = multiplier vs 24h average error rate
    """
    id: str
    name: str
    enabled: bool = True
    condition_type: str
    threshold: float
    window_minutes: int = 60
    service_id: Optional[str] = None
    cooldown_minutes: int = 30


DEFAULT_ALERT_RULES: List[AlertRule] = [
    AlertRule(
        id="high_error_rate",
        name="Más de 10 errores en 1h",
        condition_type="error_count",
        threshold=10,
        window_minutes=60,
    ),
    AlertRule(
        id="slow_response",
        name="Response > 5000ms",
        condition_type="response_ms",
        threshold=5000,
        window_minutes=15,
    ),
    AlertRule(
        id="spike",
        name="Spike de errores (3x normal)",
        condition_type="spike",
        threshold=3,
        window_minutes=60,
    ),
]


class MonitorConfig(BaseModel):
    poll_interval_seconds: int = 30
    log_tail_lines: int = 200
    error_retention_days: int = 7
    services: List[ServiceConfig] = []
    alerts: AlertConfig = AlertConfig()
    alert_rules: List[AlertRule] = []


DEFAULT_SERVICES: List[ServiceConfig] = [
    ServiceConfig(
        id="mayor",
        name="Mayor",
        url=f"http://{_SOFIA_HOST_IP}:8075/health",
        log_path="D:/mayor/backend/logs/app.log",
        enabled=True,
        restore_enabled=True,
        auto_restore=False,
    ),
    ServiceConfig(
        id="packing",
        name="Packing",
        url=f"http://{_SOFIA_HOST_IP}:8100/health",
        log_path="D:/packing/backend/logs/app.log",
        enabled=True,
        restore_enabled=True,
        auto_restore=False,
    ),
    ServiceConfig(
        id="pantalla",
        name="Pantalla",
        url=f"http://{_SOFIA_HOST_IP}:8000/health",
        log_path="D:/Pantalla/backend/logs/app.log",
        enabled=True,
        restore_enabled=True,
        auto_restore=False,
    ),
    ServiceConfig(
        id="cortana",
        name="Cortana (WhatsApp AI)",
        url=f"http://{_SOFIA_HOST_IP}:8200/health",
        log_path="D:/Cortana/backend/logs/app.log",
        enabled=True,
        auto_restore=False,
    ),
    ServiceConfig(
        id="wppconnect",
        name="WppConnect",
        url=f"http://{_SOFIA_HOST_IP}:21465/api/default/status-session",
        enabled=True,
        auto_restore=False,
    ),
]
