"""Central operator API. Never expose this router as a tenant-facing API."""
import asyncio
import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import incident_team_service as team


def operator(authorization: str = Header(default="")):
    key = os.getenv("SOFIA_API_KEY", "")
    if not key or not hmac.compare_digest(authorization, f"Bearer {key}"):
        raise HTTPException(403, "Se requiere autenticacion de operador configurada.")


router = APIRouter(prefix="/autonomy/incidents", tags=["incident-team"], dependencies=[Depends(operator)])


class DeploymentApproval(BaseModel):
    candidate_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    rollback_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    rollback_artifact: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


async def invoke(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("")
async def incidents(limit: int = Query(50, ge=1, le=100), before_id: int | None = Query(None, ge=1)):
    return await invoke(team.list_runs, limit, before_id)


@router.get("/status")
async def status():
    return await invoke(team.status)


@router.get("/{run_id}")
async def incident(run_id: int):
    return await invoke(team.get_run, run_id)


@router.post("/occurrences/{occurrence_id}")
async def enqueue(occurrence_id: int):
    return {"incident_id": await invoke(team.enqueue, occurrence_id)}


@router.post("/{run_id}/ci")
async def ci(run_id: int):
    return await invoke(team.check_ci, run_id)


@router.post("/{run_id}/approve-deployment")
async def approve(run_id: int, body: DeploymentApproval):
    return await invoke(team.approve_deployment, run_id, body.candidate_sha, body.rollback_sha, body.rollback_artifact)


@router.post("/{run_id}/cancel")
async def cancel(run_id: int):
    run = await invoke(team.get_run, run_id)
    await invoke(team.transition, run_id, run["status"], "cancelled")
    return {"ok": True}
