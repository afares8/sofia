"""
Nightly Review Service
======================
Runs once a day (first run: 2 minutes after startup, then every midnight UTC).

Flow:
  1. Collect all unresolved ERROR/CRITICAL issues from the last 24 h.
  2. Build a structured analysis prompt with every issue, its count,
     file/line detail and traceback.
  3. Invoke  `devin -p --permission-mode read-only "<prompt>"`  so that
     Devin reads the code, understands each error and writes fix proposals
     as a JSON block.  Devin is told NOT to edit any file.
  4. If Devin is unavailable or returns no parseable proposals, retry up to
     MAX_ANALYSIS_RETRIES times (with backoff).  No heuristic fallback.
  5. Save a NightlyReport in DB and send a WhatsApp summary.

When you approve a proposal from the UI:
  - Sofia invokes Devin again with write permissions to apply that one fix.
"""
import asyncio
import json
import logging
import re
import subprocess
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from app.services import db_service, whatsapp_service
from app.services.config_service import load_config

logger = logging.getLogger("sofia.nightly")

# How long to wait for Devin to finish the analysis pass
ANALYSIS_TIMEOUT_SECONDS = 600   # 10 min
# How long to wait for Devin to apply a single fix
APPLY_TIMEOUT_SECONDS = 480      # 8 min

# Retry policy for the analysis step
MAX_ANALYSIS_RETRIES = 3
RETRY_BACKOFF_SECONDS = [30, 60, 120]   # waits between retries


# ── Helpers ───────────────────────────────────────────────────────────────────


def _devin_available() -> bool:
    return shutil.which("devin") is not None or shutil.which("devin.exe") is not None


def _repo_path_for_service(service_id: str) -> Optional[str]:
    """Find the repo path for a given service_id from config."""
    cfg = load_config()
    for repo in cfg.app_repos:
        if repo.id == service_id:
            return str(Path(repo.path).expanduser())
    return None


def _run_devin(prompt: str, read_only: bool, timeout: int, cwd: Optional[str] = None) -> tuple[str, int]:
    """
    Run Devin via a temp prompt file (avoids shell arg-length limits on Windows).
    Returns (combined_output, returncode).
    read_only=True  → permission-mode auto      (auto-approves read-only tools)
    read_only=False → permission-mode dangerous  (auto-approves all tools, for applying fixes)

    Output is streamed line-by-line to the Sofia logger in real time.
    """
    import tempfile, os
    mode = "auto" if read_only else "dangerous"
    collected: list[str] = []
    tmp_path = None
    work_dir = cwd or os.getcwd()
    try:
        # Write prompt to a temp file — avoids truncation with long prompts on Windows
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            tmp_path = f.name

        cmd = ["devin", "--permission-mode", mode, "--prompt-file", tmp_path, "--print"]
        proc = subprocess.Popen(
            cmd,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout
            stdin=subprocess.DEVNULL,   # no TTY — prevents console takeover on Windows
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,  # Windows: no new console window
        )
        # Stream every line to the logger in real time
        assert proc.stdout is not None
        for line in proc.stdout:
            line_stripped = line.rstrip()
            collected.append(line_stripped)
            logger.info(f"[DEVIN] {line_stripped}")

        proc.wait(timeout=timeout)
        return "\n".join(collected), proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        msg = f"[TIMEOUT] Devin did not finish within {timeout}s."
        logger.error(f"[DEVIN] {msg}")
        return "\n".join(collected) + "\n" + msg, -1
    except Exception as exc:
        msg = f"[ERROR] Could not run Devin: {exc}"
        logger.error(f"[DEVIN] {msg}")
        return msg, -2
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _build_analysis_prompt(issues: list, period_start: str, period_end: str) -> str:
    lines = [
        "Eres el asistente nocturno de Sofia Monitor.",
        f"Período analizado: {period_start}  →  {period_end}",
        "",
        "A continuación se listan todos los errores activos de las últimas 24 horas.",
        "Tu tarea es:",
        "  1. Revisar el código fuente de cada error (sólo lectura, NO edites nada).",
        "  2. Identificar la causa raíz.",
        "  3. Redactar una propuesta de fix concreta: qué archivo, qué línea, qué cambio.",
        "",
        "IMPORTANTE: NO modifiques ningún archivo. Tu única salida debe ser un bloque",
        "JSON entre los marcadores exactos ```json_proposals y ```json_proposals con",
        "el siguiente esquema:",
        "",
        "```json_proposals",
        "[",
        "  {",
        '    "issue_id": <int>,',
        '    "service_id": "<str>",',
        '    "title": "<resumen corto del fix>",',
        '    "root_cause": "<causa raíz identificada>",',
        '    "proposal": "<descripción detallada del cambio que se debe hacer>",',
        '    "file_path": "<ruta absoluta del archivo a modificar o null>",',
        '    "line_hint": <número de línea aproximado o null>,',
        '    "confidence": "high|medium|low",',
        '    "risk": "low|medium|high"',
        "  }",
        "]",
        "```json_proposals",
        "",
        "Si un error no es del código (ej: dato de usuario, problema de SAP,",
        "credenciales expiradas) devuelve confidence=low y explícalo en root_cause.",
        "",
        "═══════════════════════ ERRORES DEL DÍA ═══════════════════════",
    ]

    for iss in issues:
        lines.append("")
        lines.append(
            f"── Issue #{iss['id']} [{iss['level']}] "
            f"servicio={iss['service_id']} count={iss['count']} ──"
        )
        lines.append(f"Mensaje: {iss['message']}")
        if iss.get("detail"):
            lines.append(f"Detalle:\n{iss['detail']}")
        if iss.get("traceback"):
            lines.append(f"Traceback:\n{iss['traceback'][:2000]}")

    lines.append("")
    lines.append("═══════════════════════════════════════════════════════")
    lines.append("Recuerda: SOLO el bloque JSON de propuestas. No edites archivos.")
    return "\n".join(lines)


def _build_apply_prompt(proposal: dict) -> str:
    return (
        f"You are an expert software engineer. Your task is to apply a specific bug fix to the codebase.\n\n"
        f"## Fix Details\n"
        f"- Service: {proposal.get('service_id')}\n"
        f"- File: {proposal.get('file_path') or 'see description below'}\n"
        f"- Line hint: {proposal.get('line_hint') or 'see description below'}\n\n"
        f"## Root Cause\n{proposal.get('root_cause', '')}\n\n"
        f"## Proposed Change\n{proposal.get('proposal', '')}\n\n"
        "## CRITICAL INSTRUCTIONS\n"
        "1. Use the Read tool to open the target file and understand the current code.\n"
        "2. Use the Edit tool (or Write tool for new files) to apply the EXACT change described above.\n"
        "3. If the file has changed since the proposal was written, adapt the fix intelligently.\n"
        "4. Do NOT modify anything else in the file unless absolutely necessary.\n"
        "5. Do NOT run git commands, tests, or build commands — only edit the file.\n"
        "6. After editing, report exactly which file(s) you modified and what changed.\n"
        "7. If you cannot apply the fix safely, say 'CANNOT_APPLY: <reason>' and stop.\n"
    )


def _extract_proposals(devin_output: str) -> List[dict]:
    """Parse the JSON proposals block from Devin's output."""
    # Primary: our custom fenced marker
    pattern = r"```json_proposals\s*(.*?)\s*```json_proposals"
    match = re.search(pattern, devin_output, re.DOTALL)
    if not match:
        # Secondary: plain ```json block containing an array
        match = re.search(r"```json\s*(\[.*?\])\s*```", devin_output, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


# ── Core analysis (with retries, Devin only) ──────────────────────────────────


async def _run_analysis_with_retries(
    issues: list,
    period_start: str,
    period_end: str,
) -> tuple[List[dict], str]:
    """
    Try up to MAX_ANALYSIS_RETRIES times to get parseable proposals from Devin.
    Returns (proposals, devin_raw_output).
    Raises RuntimeError if all attempts fail.
    """
    if not _devin_available():
        raise RuntimeError(
            "Devin CLI no encontrado en PATH. "
            "Asegúrate de que 'devin' esté instalado y accesible."
        )

    prompt = _build_analysis_prompt(issues, period_start, period_end)
    last_output = ""

    for attempt in range(1, MAX_ANALYSIS_RETRIES + 1):
        logger.info(f"[NIGHTLY] Devin analysis attempt {attempt}/{MAX_ANALYSIS_RETRIES}…")
        output, rc = await asyncio.get_event_loop().run_in_executor(
            None, _run_devin, prompt, True, ANALYSIS_TIMEOUT_SECONDS,
        )
        last_output = output
        logger.info(f"[NIGHTLY] Devin finished (rc={rc}, output_len={len(output)}).")

        proposals = _extract_proposals(output)
        if proposals:
            logger.info(f"[NIGHTLY] Extracted {len(proposals)} proposals on attempt {attempt}.")
            return proposals, output

        # No proposals — decide whether to retry
        if attempt < MAX_ANALYSIS_RETRIES:
            backoff = RETRY_BACKOFF_SECONDS[attempt - 1]
            logger.warning(
                f"[NIGHTLY] No proposals found (attempt {attempt}). "
                f"Retrying in {backoff}s…"
            )
            await asyncio.sleep(backoff)
        else:
            logger.error(
                f"[NIGHTLY] All {MAX_ANALYSIS_RETRIES} attempts failed to produce proposals."
            )

    raise RuntimeError(
        f"Devin no generó propuestas válidas después de {MAX_ANALYSIS_RETRIES} intentos. "
        f"Último output:\n{last_output[:1000]}"
    )


# ── Main entry point ──────────────────────────────────────────────────────────


async def run_nightly_review(
    force: bool = False,
    since_hours: int = 24,
) -> Optional[int]:
    """
    Analyse errors from the last `since_hours` and save a NightlyReport.
    Returns the new report ID, or None if there was nothing to analyse.
    Raises on unrecoverable error (Devin unavailable / all retries exhausted).

    `force=True` skips the "already ran today" guard.
    """
    now = datetime.utcnow()
    period_start = (now - timedelta(hours=since_hours)).isoformat()
    period_end = now.isoformat()

    # Guard: don't run twice within 20 h unless forced
    if not force:
        existing = await db_service.get_nightly_reports(limit=1)
        if existing:
            last_dt = datetime.fromisoformat(existing[0]["created_at"])
            if (now - last_dt).total_seconds() < 20 * 3600:
                logger.info("[NIGHTLY] Already ran recently, skipping. Use force=True to override.")
                return None

    # Collect issues
    issues = await db_service.get_issues(
        level=None, resolved=False, since_hours=since_hours, limit=500,
    )
    issues = [i for i in issues if i.get("level") in ("ERROR", "CRITICAL")]

    if not issues:
        logger.info("[NIGHTLY] No errors to analyse tonight.")
        return None

    logger.info(f"[NIGHTLY] Analysing {len(issues)} issues ({period_start} → {period_end})")

    # Run Devin analysis (with retries, no fallback)
    proposals, _raw_output = await _run_analysis_with_retries(issues, period_start, period_end)

    # Tag source
    for p in proposals:
        p.setdefault("source", "devin")

    proposals_json = json.dumps(proposals, ensure_ascii=False, indent=2)
    report_id = await db_service.save_nightly_report(
        period_start=period_start,
        period_end=period_end,
        issues_analyzed=len(issues),
        proposals=proposals_json,
    )

    # WhatsApp notification
    cfg = load_config()
    high_conf = sum(1 for p in proposals if p.get("confidence") == "high")
    await whatsapp_service.send_alert(
        cfg.alerts, "Sofia", "sofia", "INFO",
        "🌙 Revisión nocturna lista",
        (
            f"🌙 *Revisión nocturna completada*\n\n"
            f"📊 Issues analizados: {len(issues)}\n"
            f"💡 Propuestas: {len(proposals)} ({high_conf} de alta confianza)\n\n"
            f"Abre Sofia → Revisión Nocturna para ver y aprobar los fixes.\n"
            f"_(Reporte #{report_id})_"
        ),
    )

    logger.info(f"[NIGHTLY] Report #{report_id} saved — {len(proposals)} proposals.")
    return report_id


def _build_grouped_apply_prompt(proposals: list[dict]) -> str:
    """Build a single prompt for applying multiple proposals from the same service."""
    service_id = proposals[0].get("service_id", "unknown")
    lines = [
        f"You are an expert software engineer. Apply {len(proposals)} bug fix(es) to the '{service_id}' codebase.",
        "",
        "## CRITICAL RULES",
        "- Apply ALL fixes listed below in order.",
        "- Each fix is independent — do not let one fix interfere with another.",
        "- Use the Read tool to open each file, then Edit to apply the change.",
        "- Do NOT run git commands, tests, or builds — only edit files.",
        "- After all edits, report exactly which files were modified and how.",
        "- If any fix cannot be applied safely, say 'CANNOT_APPLY: <reason>' for that specific fix.",
        "",
        "═══════════════════════════════════════════════════════",
    ]
    for i, p in enumerate(proposals, 1):
        lines.extend([
            "",
            f"## FIX {i}/{len(proposals)}",
            f"- Issue ID: {p.get('issue_id', 'N/A')}",
            f"- Title: {p.get('title', 'Untitled')}",
            f"- File: {p.get('file_path') or 'see description'}",
            f"- Line: {p.get('line_hint') or 'see description'}",
            "",
            f"### Root Cause\n{p.get('root_cause', '')}",
            "",
            f"### Proposed Change\n{p.get('proposal', '')}",
            "",
            "───────────────────────────────────────────────────────",
        ])
    return "\n".join(lines)


async def apply_proposal(
    report_id: int,
    proposal_index: int,
    batch: bool = False,
) -> tuple[bool, str]:
    """
    Apply an approved proposal using Devin CLI (write mode).

    - batch=False (default): applies ONLY the proposal at proposal_index.
    - batch=True: groups up to 3 unapplied proposals for the SAME service
      into a single Devin session for efficiency.

    Creates proposal_run rows to track status/output/duration.
    Returns (success, output).
    """
    import time

    MAX_PROPOSALS_PER_SESSION = 3

    if not _devin_available():
        return False, "Devin CLI no encontrado en PATH. Instala 'devin' para aplicar fixes automáticamente."

    report = await db_service.get_nightly_report(report_id)
    if not report:
        return False, f"Report #{report_id} not found."

    try:
        proposals = json.loads(report.get("proposals") or "[]")
    except Exception:
        return False, "No se pudo parsear el JSON de propuestas."

    if proposal_index < 0 or proposal_index >= len(proposals):
        return False, f"Índice {proposal_index} fuera de rango (0–{len(proposals)-1})."

    target_proposal = proposals[proposal_index]
    target_service = target_proposal.get("service_id")

    # Find repo path for this service
    repo_path = _repo_path_for_service(target_service)
    if not repo_path:
        return False, f"No se encontró el repo para el servicio '{target_service}'. Configuralo en app_repos."

    # Check if repo exists
    if not Path(repo_path).exists():
        return False, f"El path del repo no existe: {repo_path}"

    # Determine what to apply: single or batch
    if batch:
        # Find other unapplied proposals for the same service
        existing_runs = await db_service.get_proposal_runs(report_id)
        applied_indices = {r["proposal_index"] for r in existing_runs if r["status"] == "success"}

        grouped = [target_proposal]
        grouped_indices = [proposal_index]
        for i, p in enumerate(proposals):
            if i == proposal_index:
                continue
            if p.get("service_id") == target_service and i not in applied_indices:
                grouped.append(p)
                grouped_indices.append(i)
                if len(grouped) >= MAX_PROPOSALS_PER_SESSION:
                    break

        if len(grouped) > 1:
            prompt = _build_grouped_apply_prompt(grouped)
            logger.info(
                f"[NIGHTLY] BATCH: Grouping {len(grouped)} proposals for service '{target_service}' "
                f"(indices {grouped_indices}) into one Devin session."
            )
        else:
            prompt = _build_apply_prompt(target_proposal)
            grouped = [target_proposal]
            grouped_indices = [proposal_index]
    else:
        # Single proposal only
        prompt = _build_apply_prompt(target_proposal)
        grouped = [target_proposal]
        grouped_indices = [proposal_index]

    # Create run records for ALL proposals before launching Devin
    run_ids: list[int] = []
    for p, idx in zip(grouped, grouped_indices):
        issue_id = p.get("issue_id")
        run_id = await db_service.create_proposal_run(
            report_id=report_id,
            proposal_index=idx,
            issue_id=int(issue_id) if issue_id else None,
            service_id=p.get("service_id"),
            title=p.get("title"),
        )
        run_ids.append(run_id)

    logger.info(
        f"[NIGHTLY] Applying proposal(s) {grouped_indices} from report #{report_id} "
        f"in repo {repo_path} (run_ids={run_ids})…"
    )

    # Capture file hashes BEFORE Devin runs (to detect actual content changes)
    from app.services.autonomy_service import _get_file_hashes
    hashes_before = _get_file_hashes(repo_path)
    logger.info(f"[NIGHTLY] Files already modified in repo before Devin: {len(hashes_before)}")

    t0 = time.monotonic()
    output, rc = await asyncio.get_event_loop().run_in_executor(
        None, _run_devin, prompt, False, APPLY_TIMEOUT_SECONDS, repo_path,
    )
    duration_s = time.monotonic() - t0
    success = rc == 0
    verifier_note = ""

    # Detect if Devin reported the fix was already applied (no changes needed)
    output_lower = output.lower()
    already_applied_indicators = [
        "already been applied",
        "no file was modified",
        "no code edit is needed",
        "already present",
        "already correct",
        "no changes needed",
        "already has the fix",
        "already handles",
        "no edit is needed",
        "no modification needed",
    ]
    devin_says_already_applied = any(ind in output_lower for ind in already_applied_indicators)

    # Compute files that Devin ACTUALLY modified during this session (by hash)
    hashes_after = _get_file_hashes(repo_path)
    new_files: list[str] = []
    for f, h in hashes_after.items():
        if f not in hashes_before or hashes_before[f] != h:
            new_files.append(f)
    new_files.sort()
    logger.info(f"[NIGHTLY] New files modified by Devin: {new_files}")

    if success and devin_says_already_applied:
        logger.info(
            f"[NIGHTLY] Devin reported fix already applied (no new edits). "
            f"Skipping verifier and marking as success."
        )
        verifier_note = "\n\nSOFIA_VERIFICATION: skipped — Devin reported fix already present in codebase."
    elif success:
        try:
            from app.services.autonomy_service import verify_current_diff
            verification = await verify_current_diff(
                f"Verificar fix aplicado para proposal(s) {grouped_indices} del report {report_id}",
                require_ai=load_config().autonomy.require_verifier,
                repo_path=repo_path,
                only_files=new_files,
            )
            verifier_note = "\n\nSOFIA_VERIFICATION:\n" + json.dumps(verification, ensure_ascii=False, indent=2)
            success = bool(verification.get("approved"))
        except Exception as exc:
            verifier_note = f"\n\nSOFIA_VERIFICATION_FAILED: {exc}"
            success = False

    logger.info(f"[NIGHTLY] Apply finished (rc={rc}, success={success}, duration={duration_s:.1f}s).")

    # Persist outcome for ALL proposals
    for p, idx, run_id in zip(grouped, grouped_indices, run_ids):
        issue_id = p.get("issue_id")
        await db_service.finish_proposal_run(
            run_id=run_id,
            success=success,
            devin_output=output + verifier_note,
            error_msg=None if success else f"Devin exit code {rc}; verifier/policy blocked" if rc == 0 else f"Devin exit code {rc}",
            duration_s=round(duration_s / len(grouped), 1),
        )

        if success and issue_id:
            try:
                await db_service.resolve_issue(int(issue_id))
                logger.info(f"[NIGHTLY] Resolved issue #{issue_id} after successful fix.")
            except Exception as exc:
                logger.warning(f"[NIGHTLY] Could not resolve issue #{issue_id}: {exc}")

    return success, output


# ── Background loop ───────────────────────────────────────────────────────────


async def nightly_loop():
    """
    Background task:
      - Runs once per day around midnight UTC.
    """
    logger.info("[NIGHTLY] Nightly review loop started.")

    while True:
        now = datetime.utcnow()
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=5, second=0, microsecond=0,
        )
        wait_seconds = max(60, (next_midnight - now).total_seconds())
        logger.info(
            f"[NIGHTLY] Next review at {next_midnight.isoformat()} — sleeping {wait_seconds/3600:.1f}h."
        )
        await asyncio.sleep(wait_seconds)

        try:
            report_id = await run_nightly_review(force=False)
            if report_id:
                logger.info(f"[NIGHTLY] Review completed → report #{report_id}")
            else:
                logger.info("[NIGHTLY] Review: nothing to report.")
        except Exception as exc:
            logger.error(f"[NIGHTLY] Review failed: {exc}", exc_info=True)

        try:
            cfg = load_config()
            if cfg.github_sync.enabled and cfg.github_sync.auto_push_at_midnight:
                from app.services.github_sync_service import sync_all_repos
                results = await sync_all_repos()
                logger.info(f"[NIGHTLY] GitHub sync completed: {results}")
        except Exception as exc:
            logger.error(f"[NIGHTLY] GitHub sync failed: {exc}", exc_info=True)

        try:
            from app.services.daily_report_service import send_daily_report
            await send_daily_report(since_hours=24)
        except Exception as exc:
            logger.error(f"[NIGHTLY] Daily activity report failed: {exc}", exc_info=True)
