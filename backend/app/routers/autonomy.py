import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.config import AppRepoConfig, AutonomyConfig, GithubSyncConfig
from app.services import db_service
from app.services.autonomy_service import create_job, policy_scan, promote_job, set_kill_switch
from app.services.config_service import load_config, save_config
from app.services.github_sync_service import sync_all_repos, sync_all_repos_background

router = APIRouter(prefix="/autonomy", tags=["autonomy"])


class KillSwitchBody(BaseModel):
    enabled: bool


class JobBody(BaseModel):
    goal: str
    service_id: Optional[str] = None
    issue_id: Optional[int] = None
    repo_id: Optional[str] = None
    autonomy_level: Optional[int] = None
    mode: str = "plan"


@router.get("/config")
async def get_autonomy_config():
    cfg = load_config()
    return {"autonomy": cfg.autonomy, "app_repos": cfg.app_repos, "github_sync": cfg.github_sync}


@router.put("/config/autonomy", response_model=AutonomyConfig)
async def update_autonomy_config(body: AutonomyConfig):
    cfg = load_config()
    cfg.autonomy = body
    save_config(cfg)
    return body


@router.put("/config/github-sync", response_model=GithubSyncConfig)
async def update_github_sync_config(body: GithubSyncConfig):
    cfg = load_config()
    cfg.github_sync = body
    save_config(cfg)
    return body


@router.put("/config/app-repos", response_model=list[AppRepoConfig])
async def update_app_repos(body: list[AppRepoConfig]):
    cfg = load_config()
    cfg.app_repos = body
    save_config(cfg)
    return body


@router.post("/kill-switch")
async def kill_switch(body: KillSwitchBody):
    await set_kill_switch(body.enabled)
    return {"ok": True, "kill_switch": body.enabled}


@router.get("/jobs")
async def list_jobs(limit: int = 50):
    return await db_service.get_ai_jobs(limit=limit)


@router.get("/jobs/{job_id}")
async def get_job(job_id: int):
    job = await db_service.get_ai_job(job_id)
    if not job:
        raise HTTPException(404, f"Job #{job_id} not found.")
    return job


@router.post("/jobs")
async def start_job(body: JobBody):
    if not body.goal.strip():
        raise HTTPException(400, "goal is required.")
    if body.mode not in {"plan", "fix"}:
        raise HTTPException(400, "mode must be 'plan' or 'fix'.")
    job_id = await create_job(
        body.goal,
        service_id=body.service_id,
        issue_id=body.issue_id,
        repo_id=body.repo_id,
        autonomy_level=body.autonomy_level,
        mode=body.mode,
    )
    return {"ok": True, "job_id": job_id}


@router.post("/jobs/{job_id}/promote")
async def promote_job_endpoint(job_id: int):
    """Promote a verified sandbox fix to the real repo (push branch + open PR)."""
    try:
        return await promote_job(job_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Promoción falló: {exc}")


@router.get("/actions")
async def list_actions(limit: int = 50):
    return await db_service.get_action_runs(limit=limit)


@router.get("/audit")
async def list_audit_events(
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    limit: int = 100,
):
    return await db_service.get_audit_events(entity_type=entity_type, entity_id=entity_id, limit=limit)


@router.get("/github-sync/runs")
async def list_github_sync_runs(limit: int = 50):
    return await db_service.get_github_sync_runs(limit=limit)


@router.post("/github-sync/run")
async def trigger_github_sync(background: bool = True):
    if background:
        asyncio.create_task(sync_all_repos_background())
        return {"ok": True, "message": "GitHub sync iniciado en background."}
    return {"ok": True, "results": await sync_all_repos()}


@router.get("/daily-report")
async def preview_daily_report(since_hours: int = 24):
    from app.services.daily_report_service import build_daily_report
    return {"report": await build_daily_report(since_hours=since_hours)}


@router.post("/daily-report/send")
async def send_daily_report_now(since_hours: int = 24):
    from app.services.daily_report_service import build_daily_report, send_daily_report
    report = await build_daily_report(since_hours=since_hours)
    sent = await send_daily_report(since_hours=since_hours)
    return {"sent": sent, "report": report}


@router.get("/policy-scan")
async def run_policy_scan():
    import pathlib
    repo_path = str(pathlib.Path(__file__).resolve().parents[3])
    return policy_scan(repo_path)
