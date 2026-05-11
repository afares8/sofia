"""
Event / error models stored in SQLite.
"""
from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ErrorEvent(BaseModel):
    id: Optional[int] = None
    service_id: str
    service_name: str
    level: str          # ERROR | WARNING | CRITICAL
    message: str
    detail: Optional[str] = None
    traceback: Optional[str] = None
    source: str         # "active" (SDK pushed) | "passive" (log tail)
    timestamp: datetime = None
    notified: bool = False

    class Config:
        from_attributes = True


class ServiceStatus(BaseModel):
    id: str
    name: str
    status: str         # "up" | "down" | "unknown"
    status_code: Optional[int] = None
    response_ms: Optional[float] = None
    last_checked: Optional[datetime] = None
    last_seen_up: Optional[datetime] = None
    enabled: bool = True
