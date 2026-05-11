from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ErrorEvent(BaseModel):
    id: Optional[int] = None
    service_id: str
    service_name: str
    level: str
    message: str
    detail: Optional[str] = None
    traceback: Optional[str] = None
    url: Optional[str] = None
    user_info: Optional[str] = None
    source: str = "active"
    timestamp: Optional[datetime] = None
    notified: bool = False

    class Config:
        from_attributes = True


class ServiceStatus(BaseModel):
    id: str
    name: str
    status: str
    status_code: Optional[int] = None
    response_ms: Optional[float] = None
    last_checked: Optional[datetime] = None
    last_seen_up: Optional[datetime] = None
    enabled: bool = True
