"""
Models for the WhatsApp-triggered restore workflow.
"""
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from enum import Enum


class RestoreStatus(str, Enum):
    PENDING    = "pending"     # Waiting for user confirmation via WhatsApp
    CONFIRMED  = "confirmed"   # User said SI, running restore script
    RUNNING    = "running"     # Script is executing
    SUCCESS    = "success"     # Service came back up
    FAILED     = "failed"      # Script ran but service didn't come back
    REJECTED   = "rejected"    # User said NO
    EXPIRED    = "expired"     # No response within timeout


class PendingRestore(BaseModel):
    service_id: str
    service_name: str
    status: RestoreStatus = RestoreStatus.PENDING
    requested_at: datetime
    confirmed_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result_message: Optional[str] = None
    devin_output: Optional[str] = None      # last lines of devin session output
    retry_count: int = 0
    trigger_mode: str = "manual"            # "auto" | "manual"
    restore_method: Optional[str] = None    # "devin" | "ps1_script"
    db_id: Optional[int] = None             # row id in the restores table
