"""
Nightly review router.

GET  /nightly/                          → list reports
GET  /nightly/{id}                      → single report detail
GET  /nightly/{id}/runs                 → per-proposal apply runs for this report
POST /nightly/run                       → trigger review immediately
POST /nightly/{id}/approve              → mark report approved (no apply)
POST /nightly/{id}/approve/{index}      → approve+apply a single proposal
POST /nightly/{id}/reject               → reject whole report
"""
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import db_service
from app.services.nightly_review_service import run_nightly_review, apply_proposal

router = APIRouter(prefix="/nightly", tags=["nightly"])
_log = logging.getLogger("sofia.nightly")


class RunParams(BaseModel):
    force: bool = True
    since_hours: int = 24


class ApproveBody(BaseModel):
    notes: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_proposals(row: dict) -> list:
    try:
        return json.loads(row.get("proposals") or "[]")
    except Exception:
        return []


def _format_report(row: dict) -> dict:
    row = dict(row)
    row["proposals"] = _parse_proposals(row)
    return row


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/")
async def list_reports(limit: int = 30):
    rows = await db_service.get_nightly_reports(limit=limit)
    return [_format_report(r) for r in rows]


@router.get("/{report_id}")
async def get_report(report_id: int):
    row = await db_service.get_nightly_report(report_id)
    if not row:
        raise HTTPException(404, f"Report #{report_id} not found.")
    return _format_report(row)


@router.get("/{report_id}/runs")
async def get_proposal_runs(report_id: int):
    """Return all per-proposal apply runs for a report."""
    row = await db_service.get_nightly_report(report_id)
    if not row:
        raise HTTPException(404, f"Report #{report_id} not found.")
    runs = await db_service.get_proposal_runs(report_id)
    return runs


@router.post("/run")
async def trigger_run(params: RunParams):
    """Manually trigger the nightly review (runs in background)."""
    import asyncio
    asyncio.create_task(_run_background(params.force, params.since_hours))
    return {"ok": True, "message": "Revisión nocturna iniciada en background. Recargá en ~2 min."}


async def _run_background(force: bool, since_hours: int):
    try:
        report_id = await run_nightly_review(force=force, since_hours=since_hours)
        if report_id:
            _log.info(f"[NIGHTLY] Background run finished → report #{report_id}")
    except Exception as exc:
        _log.error(f"[NIGHTLY] Background run failed: {exc}", exc_info=True)


@router.post("/{report_id}/approve")
async def approve_report(report_id: int, body: ApproveBody):
    """
    Mark the whole report as approved — does NOT apply any fix.
    Each proposal must be applied individually via /approve/{index}.
    """
    row = await db_service.get_nightly_report(report_id)
    if not row:
        raise HTTPException(404, f"Report #{report_id} not found.")
    if row["status"] not in ("pending",):
        raise HTTPException(400, f"El reporte ya está en estado '{row['status']}'.")

    await db_service.update_nightly_report(
        report_id,
        status="approved",
        approved_at=datetime.utcnow().isoformat(),
        notes=body.notes,
    )
    return {"ok": True, "report_id": report_id, "status": "approved"}


@router.post("/{report_id}/approve/{proposal_index}")
async def approve_and_apply_proposal(report_id: int, proposal_index: int):
    """
    Approve and immediately apply a SINGLE proposal via Codex CLI.
    Only applies the proposal at proposal_index, nothing else.
    The apply runs in the background; poll /runs to watch progress.
    """
    row = await db_service.get_nightly_report(report_id)
    if not row:
        raise HTTPException(404, f"Report #{report_id} not found.")
    if row["status"] == "rejected":
        raise HTTPException(400, "El reporte fue rechazado.")

    proposals = _parse_proposals(row)
    if proposal_index < 0 or proposal_index >= len(proposals):
        raise HTTPException(400, f"Índice {proposal_index} fuera de rango (0–{len(proposals)-1}).")

    # Auto-approve if still pending
    if row["status"] == "pending":
        await db_service.update_nightly_report(
            report_id,
            status="approved",
            approved_at=datetime.utcnow().isoformat(),
        )

    import asyncio
    asyncio.create_task(_apply_background(report_id, proposal_index, batch=False))
    return {
        "ok": True,
        "report_id": report_id,
        "proposal_index": proposal_index,
        "batch": False,
        "message": "Fix individual lanzado en background. Consultá /runs para ver el progreso.",
    }


@router.post("/{report_id}/apply-batch/{proposal_index}")
async def apply_batch_for_service(report_id: int, proposal_index: int):
    """
    Apply the selected proposal PLUS other unapplied proposals for the SAME
    service, grouped into a single Codex session (max 3 per session).
    More efficient but applies multiple fixes at once.
    """
    row = await db_service.get_nightly_report(report_id)
    if not row:
        raise HTTPException(404, f"Report #{report_id} not found.")
    if row["status"] == "rejected":
        raise HTTPException(400, "El reporte fue rechazado.")

    proposals = _parse_proposals(row)
    if proposal_index < 0 or proposal_index >= len(proposals):
        raise HTTPException(400, f"Índice {proposal_index} fuera de rango (0–{len(proposals)-1}).")

    # Auto-approve if still pending
    if row["status"] == "pending":
        await db_service.update_nightly_report(
            report_id,
            status="approved",
            approved_at=datetime.utcnow().isoformat(),
        )

    import asyncio
    asyncio.create_task(_apply_background(report_id, proposal_index, batch=True))

    service_id = proposals[proposal_index].get("service_id")
    return {
        "ok": True,
        "report_id": report_id,
        "proposal_index": proposal_index,
        "batch": True,
        "service_id": service_id,
        "message": f"Batch fix para '{service_id}' lanzado. Puede incluir hasta 3 proposals del mismo servicio.",
    }


async def _apply_background(report_id: int, proposal_index: int, batch: bool = False):
    try:
        success, output = await apply_proposal(report_id, proposal_index, batch=batch)
        # Update the report-level status based on this run's result.
        # We keep it 'approved' (not 'applied') so the user can still apply more fixes;
        # only flip to 'applied' if at least one fix succeeded.
        if success:
            await db_service.update_nightly_report(
                report_id,
                status="applied",
                applied_at=datetime.utcnow().isoformat(),
            )
        else:
            # Only mark failed if it wasn't already marked applied by a previous run
            row = await db_service.get_nightly_report(report_id)
            if row and row["status"] not in ("applied",):
                await db_service.update_nightly_report(
                    report_id,
                    status="apply_failed",
                )
    except Exception as exc:
        _log.error(f"[NIGHTLY] Apply background error: {exc}", exc_info=True)


@router.post("/{report_id}/reject")
async def reject_report(report_id: int, body: ApproveBody):
    row = await db_service.get_nightly_report(report_id)
    if not row:
        raise HTTPException(404, f"Report #{report_id} not found.")
    if row["status"] not in ("pending", "approved"):
        raise HTTPException(400, f"El reporte ya está en estado '{row['status']}'.")

    await db_service.update_nightly_report(
        report_id,
        status="rejected",
        rejected_at=datetime.utcnow().isoformat(),
        notes=body.notes,
    )
    return {"ok": True, "report_id": report_id, "status": "rejected"}
