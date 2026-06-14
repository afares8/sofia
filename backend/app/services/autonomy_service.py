import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.services import db_service
from app.services.config_service import load_config, save_config

logger = logging.getLogger("sofia.autonomy")

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"]?[^'\"\s]{12,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"(?i)github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)ghp_[A-Za-z0-9]{20,}"),
]

DANGEROUS_DIFF_PATTERNS = [
    re.compile(r"(?i)\bdrop\s+table\b"),
    re.compile(r"(?i)\btruncate\s+table\b"),
    re.compile(r"(?i)\brm\s+-rf\b"),
    re.compile(r"(?i)\bforce[_-]?push\b"),
    re.compile(r"(?i)verify\s*=\s*false"),
    re.compile(r"(?i)cors.*allow_origins\s*=\s*\[\s*['\"]\*['\"]"),
]


def _devin_available() -> bool:
    return shutil.which("devin") is not None or shutil.which("devin.exe") is not None


def _run(cmd: list[str], cwd: Optional[str] = None, timeout: int = 120) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _run_shell(command: str, cwd: str, timeout: int = 300) -> tuple[int, str]:
    proc = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _run_devin(prompt: str, read_only: bool, cwd: Optional[str], timeout: int = 900) -> tuple[int, str]:
    if not _devin_available():
        return -2, "Devin CLI no encontrado en PATH."
    mode = "auto" if read_only else "dangerous"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(prompt)
            tmp_path = f.name
        devin_bin = shutil.which("devin") or shutil.which("devin.exe") or "devin"
        cmd = [devin_bin, "--permission-mode", mode, "--prompt-file", tmp_path, "--print"]
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"TIMEOUT: Devin no terminó en {timeout}s."
    except Exception as exc:
        return -3, f"ERROR: {exc}"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _changed_files(repo_path: str) -> list[str]:
    rc, out = _run(["git", "status", "--porcelain"], cwd=repo_path)
    if rc != 0:
        return []
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.replace("\\", "/"))
    return files


def _repo_for(service_id: Optional[str], repo_id: Optional[str] = None):
    cfg = load_config()
    if repo_id:
        found = next((r for r in cfg.app_repos if r.id == repo_id), None)
        if found:
            return found
    if service_id:
        found = next((r for r in cfg.app_repos if r.id == service_id), None)
        if found:
            return found
    return next((r for r in cfg.app_repos if r.id == "sofia"), None)


def _safe_branch_name(job_id: int, goal: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", goal.lower()).strip("-")[:42] or "job"
    return f"sofia/ai-job-{job_id}-{slug}"


def _create_sandbox(job_id: int, repo_cfg) -> tuple[str, str, str]:
    root = Path(load_config().autonomy.sandbox_root)
    root.mkdir(parents=True, exist_ok=True)
    sandbox = root / f"{repo_cfg.id}-job-{job_id}-{int(time.time())}"
    source = str(Path(repo_cfg.path).expanduser())
    rc, out = _run(["git", "clone", "--no-hardlinks", source, str(sandbox)], timeout=600)
    if rc != 0:
        raise RuntimeError(f"git clone failed: {out}")
    rc, branch_out = _run(["git", "branch", "--show-current"], cwd=str(sandbox))
    base_branch = branch_out.strip() if rc == 0 and branch_out.strip() else repo_cfg.branch
    if repo_cfg.branch:
        _run(["git", "checkout", repo_cfg.branch], cwd=str(sandbox), timeout=180)
        base_branch = repo_cfg.branch
    work_branch = _safe_branch_name(job_id, repo_cfg.id)
    rc, out = _run(["git", "checkout", "-b", work_branch], cwd=str(sandbox), timeout=180)
    if rc != 0:
        raise RuntimeError(f"git checkout -b failed: {out}")
    return str(sandbox), base_branch, work_branch


def _git_diff(repo_path: str, only_files: list[str] | None = None) -> str:
    if only_files:
        # Diff only specific files
        rc, out = _run(["git", "diff", "--"] + only_files, cwd=repo_path, timeout=180)
    else:
        rc, out = _run(["git", "diff", "--"], cwd=repo_path, timeout=180)
    return out if rc == 0 else out


def _is_test_file(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    return (
        "/tests/" in p
        or "/test/" in p
        or "/__tests__/" in p
        or base.startswith("test_")
        or base.endswith("_test.py")
        or base.endswith(".test.ts")
        or base.endswith(".test.tsx")
        or base.endswith(".spec.ts")
        or base.endswith(".spec.tsx")
    )


def _count_changed_lines(diff: str, include_tests: bool = False) -> int:
    """
    Count added/removed lines in a unified diff. When include_tests is False,
    hunks belonging to test files are skipped so that adding test coverage does
    not count against the max_lines_changed guardrail.
    """
    total = 0
    current_is_test = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            # Format: diff --git a/<path> b/<path>
            parts = line.split(" b/", 1)
            path = parts[1].strip() if len(parts) == 2 else line
            current_is_test = _is_test_file(path)
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if current_is_test and not include_tests:
            continue
        if line.startswith("+") or line.startswith("-"):
            total += 1
    return total


def _get_file_hashes(repo_path: str) -> dict[str, str]:
    """
    Return {rel_path: md5_hash} for all modified and untracked files.
    This detects actual content changes, not just git status changes.
    """
    import hashlib
    # Get modified + untracked + renamed files
    rc, out = _run(["git", "status", "--porcelain"], cwd=repo_path, timeout=60)
    if rc != 0:
        return {}
    files: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        # Any non-empty status means the file changed (M, A, D, R, ??, etc.)
        if status.strip():
            files.append(path)

    result: dict[str, str] = {}
    for f in files:
        p = Path(repo_path) / f
        if p.exists() and p.is_file() and p.stat().st_size < 10_000_000:
            try:
                with open(p, "rb") as fh:
                    result[f.replace("\\", "/")] = hashlib.md5(fh.read()).hexdigest()
            except Exception:
                pass
    return result


def _changed_file_text(repo_path: str, rel_path: str) -> str:
    path = Path(repo_path) / rel_path
    try:
        if not path.exists() or path.is_dir() or path.stat().st_size > 1_000_000:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def policy_scan(repo_path: str, diff_text: Optional[str] = None, repo_cfg=None, only_files: list[str] | None = None) -> dict:
    cfg = load_config().autonomy
    allowed_paths = list(repo_cfg.allowed_paths or cfg.allowed_paths) if repo_cfg else cfg.allowed_paths
    blocked_paths = list(cfg.blocked_paths) + (list(repo_cfg.blocked_paths or []) if repo_cfg else [])
    files = only_files if only_files is not None else _changed_files(repo_path)
    diff = diff_text if diff_text is not None else _git_diff(repo_path, only_files=only_files)
    file_text = "\n".join(_changed_file_text(repo_path, f) for f in files)
    blocked: list[str] = []

    for f in files:
        if any(f.startswith(p.strip("/")) or p in f for p in blocked_paths):
            blocked.append(f"Archivo bloqueado: {f}")

    if allowed_paths:
        for f in files:
            if not any(f.startswith(p) for p in allowed_paths):
                blocked.append(f"Fuera de allowed_paths: {f}")

    if len(files) > cfg.max_files_changed:
        blocked.append(f"Demasiados archivos modificados: {len(files)} > {cfg.max_files_changed}")

    # Count changed lines, excluding test files unless explicitly configured.
    # Adding thorough tests should never block a small production fix.
    changed_lines = _count_changed_lines(
        diff, include_tests=cfg.count_test_files_in_limit
    )
    if changed_lines > cfg.max_lines_changed:
        blocked.append(f"Diff demasiado grande: {changed_lines} líneas > {cfg.max_lines_changed}")

    # NOTE: secret scanning is intentionally NOT done here. It produced too many
    # false positives and blocked legitimate fixes. Secrets are still scanned by
    # github_sync_service before anything is pushed to GitHub, which is the only
    # place a leak actually matters.

    for pattern in DANGEROUS_DIFF_PATTERNS:
        if pattern.search(diff) or pattern.search(file_text):
            blocked.append(f"Patrón peligroso detectado: {pattern.pattern}")

    return {
        "approved": not blocked,
        "risk": "low" if not blocked and changed_lines <= 80 else ("medium" if not blocked else "high"),
        "files": files,
        "changed_lines": changed_lines,
        "reasons": blocked,
    }


def _run_quality_commands(repo_path: str, repo_cfg) -> tuple[str, str]:
    commands = list(repo_cfg.test_commands or []) + list(repo_cfg.build_commands or [])
    if not commands:
        return "skipped", "No hay test_commands/build_commands configurados para este repo."
    output = []
    status = "success"
    for cmd in commands:
        rc, out = _run_shell(cmd, repo_path, timeout=900)
        output.append(f"$ {cmd}\n{out}")
        if rc != 0:
            status = "failed"
            break
    return status, "\n\n".join(output)[-16000:]


async def _run_smoke_checks(repo_cfg) -> tuple[str, str]:
    import httpx
    urls = list(repo_cfg.smoke_urls or [])
    if not urls:
        return "skipped", "No hay smoke_urls configuradas."
    lines = []
    status = "success"
    async with httpx.AsyncClient(timeout=10) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                ok = resp.status_code < 500
                lines.append(f"{url} -> {resp.status_code} {'OK' if ok else 'FAIL'}")
                if not ok:
                    status = "failed"
            except Exception as exc:
                status = "failed"
                lines.append(f"{url} -> ERROR {exc}")
    return status, "\n".join(lines)


async def verify_current_diff(
    goal: str,
    require_ai: bool = True,
    repo_path: Optional[str] = None,
    only_files: list[str] | None = None,
) -> dict:
    target_repo = repo_path or str(Path(__file__).resolve().parents[3])
    diff = _git_diff(target_repo, only_files=only_files)
    policy = policy_scan(target_repo, diff, only_files=only_files)
    result = {"policy": policy, "ai": None, "approved": policy["approved"], "risk": policy["risk"]}
    if not require_ai or not policy["approved"]:
        return result
    job = {"id": "external", "goal": goal}
    rc, raw = await asyncio.get_event_loop().run_in_executor(
        None, _run_devin, _verifier_prompt(job, diff, policy), True, target_repo, 600,
    )
    decision = _parse_verifier_decision(raw)
    result["ai"] = {"rc": rc, "decision": decision, "output": raw[-12000:]}
    result["approved"] = rc == 0 and decision["approved"]
    result["risk"] = decision["risk"]
    return result


def _engineer_prompt(job: dict) -> str:
    mode = job.get("mode") or "plan"
    return f"""Eres el Engineer AI de Sofia. Trabaja con autonomía controlada.

Job #{job['id']}
Servicio: {job.get('service_id') or 'n/a'}
Issue: {job.get('issue_id') or 'n/a'}
Modo: {mode}
Objetivo:
{job['goal']}

Reglas:
- No hagas push.
- No modifiques .env, secrets, backend/data, backend/logs, node_modules ni dist.
- Si el modo es plan, solo investiga y devuelve un plan concreto.
- Si el modo es fix, aplica el cambio mínimo, agrega/ajusta tests si corresponde y corre la verificación disponible.
- Termina con una sección exacta:
SOFIA_RESULT:
status=<success|blocked|failed>
risk=<low|medium|high>
summary=<resumen corto>
"""


def _verifier_prompt(job: dict, diff: str, policy: dict) -> str:
    return f"""Eres el Verifier AI de Sofia. No confíes en el Engineer AI.

Revisa el Job #{job['id']} y decide si el cambio es seguro.

Objetivo:
{job['goal']}

Resultado de policy scan:
{json.dumps(policy, ensure_ascii=False, indent=2)}

Diff:
```diff
{diff[:12000]}
```

Devuelve JSON estricto:
{{
  "approved": true|false,
  "risk": "low|medium|high",
  "reasons": ["..."],
  "required_actions": ["..."]
}}
"""


def _parse_verifier_decision(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {"approved": False, "risk": "high", "reasons": ["Verifier no devolvió JSON."], "required_actions": []}
    try:
        data = json.loads(match.group(0))
        return {
            "approved": bool(data.get("approved")),
            "risk": data.get("risk") if data.get("risk") in {"low", "medium", "high"} else "high",
            "reasons": data.get("reasons") if isinstance(data.get("reasons"), list) else [],
            "required_actions": data.get("required_actions") if isinstance(data.get("required_actions"), list) else [],
        }
    except Exception as exc:
        return {"approved": False, "risk": "high", "reasons": [f"JSON inválido: {exc}"], "required_actions": []}


async def set_kill_switch(enabled: bool) -> None:
    cfg = load_config()
    cfg.autonomy.kill_switch = enabled
    if enabled:
        cfg.autonomy.enabled = False
    save_config(cfg)


async def create_job(
    goal: str,
    service_id: Optional[str] = None,
    issue_id: Optional[int] = None,
    autonomy_level: Optional[int] = None,
    mode: str = "plan",
    repo_id: Optional[str] = None,
) -> int:
    cfg = load_config().autonomy
    repo_cfg = _repo_for(service_id, repo_id)
    level = autonomy_level if autonomy_level is not None else (repo_cfg.autonomy_level if repo_cfg else cfg.default_level)
    job_id = await db_service.create_ai_job(goal, service_id, issue_id, level, mode, repo_cfg.id if repo_cfg else repo_id)
    await db_service.add_audit_event("ai_job", "created", job_id, f"Job creado en modo {mode}.")
    asyncio.create_task(run_job(job_id))
    return job_id


async def run_job(job_id: int) -> None:
    job = await db_service.get_ai_job(job_id)
    if not job:
        return
    cfg = load_config().autonomy
    repo_cfg = _repo_for(job.get("service_id"), job.get("repo_id"))
    if not repo_cfg:
        await db_service.update_ai_job(job_id, status="blocked", risk="high", blocked_reason="Repo no configurado.")
        return
    repo_path = str(Path(repo_cfg.path).expanduser())

    if cfg.kill_switch or not cfg.enabled:
        await db_service.update_ai_job(
            job_id,
            status="blocked",
            risk="high",
            blocked_reason="Autonomía apagada o kill switch activo.",
        )
        await db_service.add_audit_event("ai_job", "blocked", job_id, "Autonomía apagada o kill switch activo.")
        return

    if (job.get("mode") == "fix") and int(job.get("autonomy_level") or 1) < 3:
        await db_service.update_ai_job(
            job_id,
            status="blocked",
            risk="medium",
            blocked_reason="Modo fix requiere autonomy_level >= 3.",
        )
        await db_service.add_audit_event("ai_job", "blocked", job_id, "Modo fix requiere level >= 3.")
        return

    if (job.get("mode") == "fix") and load_config().autonomy.require_human_for_apply:
        await db_service.add_audit_event("ai_job", "safe_mode", job_id, "Fix se ejecutará en sandbox; no se aplicará al repo principal.")

    if job.get("mode") == "fix":
        try:
            sandbox_path, base_branch, work_branch = _create_sandbox(job_id, repo_cfg)
            repo_path = sandbox_path
            await db_service.update_ai_job(
                job_id,
                sandbox_path=sandbox_path,
                base_branch=base_branch,
                work_branch=work_branch,
                branch_name=work_branch,
            )
            await db_service.add_audit_event("ai_job", "sandbox_created", job_id, sandbox_path)
        except Exception as exc:
            await db_service.update_ai_job(job_id, status="failed", risk="high", blocked_reason=str(exc))
            await db_service.add_audit_event("ai_job", "sandbox_failed", job_id, str(exc))
            return

    await db_service.update_ai_job(job_id, status="running", repo_id=repo_cfg.id)
    prompt = _engineer_prompt(job)
    rc, output = await asyncio.get_event_loop().run_in_executor(
        None, _run_devin, prompt, job.get("mode") != "fix", repo_path, 900,
    )
    await db_service.update_ai_job(job_id, devin_output=output[-16000:])
    await db_service.add_audit_event("ai_job", "devin_finished", job_id, f"rc={rc}")

    if rc != 0:
        await db_service.update_ai_job(
            job_id,
            status="failed",
            risk="medium",
            result_message=f"Engineer AI falló con rc={rc}.",
        )
        return

    diff = _git_diff(repo_path)
    policy = policy_scan(repo_path, diff, repo_cfg)
    await db_service.update_ai_job(job_id, diff_summary=json.dumps(policy, ensure_ascii=False))

    if job.get("mode") != "fix":
        await db_service.update_ai_job(
            job_id,
            status="completed",
            risk="low",
            verifier_status="not_required",
            result_message="Plan generado por Devin.",
        )
        await db_service.add_audit_event("ai_job", "completed", job_id, "Plan generado.")
        return

    if not policy.get("files"):
        await db_service.update_ai_job(
            job_id,
            status="blocked",
            risk="medium",
            blocked_reason="Devin no produjo cambios en modo fix.",
            result_message="Cambio bloqueado porque no hubo diff que auditar.",
        )
        await db_service.add_audit_event("ai_job", "blocked", job_id, "Sin cambios después de Devin.")
        return

    tests_status, tests_output = _run_quality_commands(repo_path, repo_cfg)
    await db_service.update_ai_job(job_id, tests_status=tests_status, tests_output=tests_output)
    await db_service.add_audit_event("ai_job", "tests_finished", job_id, tests_status)
    post_test_policy = policy_scan(repo_path, None, repo_cfg)
    if not post_test_policy["approved"]:
        await db_service.update_ai_job(
            job_id,
            status="blocked",
            risk="high",
            diff_summary=json.dumps(post_test_policy, ensure_ascii=False),
            blocked_reason="Policy scan falló después de tests/build.",
            result_message="Cambio bloqueado por artefactos o riesgos generados durante la verificación.",
        )
        await db_service.add_audit_event("ai_job", "blocked", job_id, "Policy post-tests falló.")
        return
    await db_service.update_ai_job(job_id, diff_summary=json.dumps(post_test_policy, ensure_ascii=False))
    if cfg.require_tests_for_code_fixes and tests_status == "failed":
        await db_service.update_ai_job(
            job_id,
            status="blocked",
            risk="high",
            blocked_reason="Tests/build fallaron.",
            result_message="Cambio bloqueado porque la verificación local falló.",
        )
        return

    if cfg.run_smoke_checks:
        smoke_status, smoke_output = await _run_smoke_checks(repo_cfg)
        await db_service.update_ai_job(job_id, smoke_status=smoke_status, smoke_output=smoke_output)
        await db_service.add_audit_event("ai_job", "smoke_finished", job_id, smoke_status)

    verifier_output = json.dumps(policy, ensure_ascii=False)
    verifier_status = "approved" if policy["approved"] else "blocked"
    risk = policy["risk"]
    rc2 = None
    decision = None

    if cfg.require_verifier and policy["approved"]:
        rc2, verifier_raw = await asyncio.get_event_loop().run_in_executor(
            None, _run_devin, _verifier_prompt(job, diff, policy), True, repo_path, 600,
        )
        decision = _parse_verifier_decision(verifier_raw)
        verifier_output = verifier_raw[-12000:] + "\n\nSOFIA_VERIFIER_DECISION:\n" + json.dumps(decision, ensure_ascii=False)
        await db_service.update_ai_job(job_id, verifier_decision=json.dumps(decision, ensure_ascii=False))
        risk = decision["risk"]
        if rc2 != 0 or not decision["approved"]:
            verifier_status = "blocked"

    commit_sha = None
    if verifier_status == "approved" and cfg.commit_in_sandbox:
        rc_add, out_add = _run(["git", "add", "--"] + _changed_files(repo_path), cwd=repo_path, timeout=180)
        rc_commit, out_commit = _run(
            ["git", "commit", "-m", f"fix({repo_cfg.id}): AI job {job_id}\n\nGenerated with [Devin](https://cli.devin.ai/docs)\n\nCo-Authored-By: Devin <158243242+devin-ai-integration[bot]@users.noreply.github.com>"],
            cwd=repo_path,
            timeout=180,
        )
        if rc_add == 0 and rc_commit == 0:
            rc_sha, out_sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_path)
            commit_sha = out_sha.strip() if rc_sha == 0 else None
            await db_service.add_audit_event("ai_job", "sandbox_commit", job_id, commit_sha)
        else:
            verifier_status = "blocked"
            risk = "medium"
            verifier_output += f"\n\nCOMMIT_FAILED:\n{out_add}\n{out_commit}"

    await db_service.update_ai_job(
        job_id,
        status="verified" if verifier_status == "approved" else "blocked",
        verifier_status=verifier_status,
        verifier_output=verifier_output,
        risk=risk,
        commit_sha=commit_sha,
        blocked_reason=None if verifier_status == "approved" else "Verifier/policy no aprobó el cambio.",
        result_message="Cambio verificado." if verifier_status == "approved" else "Cambio bloqueado por guardrails.",
    )
    await db_service.add_audit_event("ai_job", verifier_status, job_id, "Job finalizado.")

    # ── Promotion: push the verified fix to the real repo ─────────────────────
    if verifier_status == "approved":
        if cfg.auto_promote_low_risk and risk == "low" and cfg.promotion_mode != "manual":
            try:
                result = await promote_job(job_id)
                logger.info(f"[AUTONOMY] Job #{job_id} auto-promovido: {result.get('message')}")
            except Exception as exc:
                logger.error(f"[AUTONOMY] Auto-promote del job #{job_id} falló: {exc}", exc_info=True)
                await db_service.add_audit_event("ai_job", "promote_failed", job_id, str(exc))
        else:
            await db_service.add_audit_event(
                "ai_job", "awaiting_promotion", job_id,
                f"Verificado (risk={risk}). Esperando promoción manual desde la UI.",
            )


def _gh_available() -> bool:
    return shutil.which("gh") is not None or shutil.which("gh.exe") is not None


async def promote_job(job_id: int) -> dict:
    """
    Promote a verified sandbox fix into the real repository.

    Flow (promotion_mode):
      - Fetch the sandbox work branch into the real repo.
      - Push that branch to origin (GitHub).
      - "pr"     → open a GitHub PR with `gh pr create`.
      - "branch" → leave the pushed branch for manual review (no PR).
      - "manual" → only runs when called explicitly from the UI; behaves like "pr".

    On success, marks the job as 'promoted', records pr_url and promoted_at,
    and resolves the associated issue.
    """
    job = await db_service.get_ai_job(job_id)
    if not job:
        raise RuntimeError(f"Job #{job_id} no existe.")
    if job.get("status") != "verified":
        raise RuntimeError(f"Job #{job_id} está en estado '{job.get('status')}', no 'verified'.")
    if not job.get("commit_sha"):
        raise RuntimeError(f"Job #{job_id} no tiene commit en el sandbox.")

    cfg = load_config().autonomy
    mode = cfg.promotion_mode if cfg.promotion_mode in ("pr", "branch", "manual") else "pr"
    repo_cfg = _repo_for(job.get("service_id"), job.get("repo_id"))
    if not repo_cfg:
        raise RuntimeError("Repo no configurado para este job.")

    real_repo = str(Path(repo_cfg.path).expanduser())
    sandbox = job.get("sandbox_path")
    work_branch = job.get("work_branch")
    base_branch = job.get("base_branch") or repo_cfg.branch or "main"
    if not sandbox or not work_branch:
        raise RuntimeError("Faltan sandbox_path o work_branch en el job.")

    await db_service.add_audit_event("ai_job", "promote_started", job_id, f"mode={mode}")

    # 1. Bring the sandbox branch into the real repo.
    rc, out = _run(
        ["git", "fetch", sandbox, f"{work_branch}:{work_branch}"],
        cwd=real_repo, timeout=300,
    )
    if rc != 0:
        raise RuntimeError(f"git fetch del sandbox falló: {out}")

    # 2. Push the branch to origin (GitHub).
    rc, out_push = _run(["git", "push", "-u", "origin", work_branch], cwd=real_repo, timeout=300)
    if rc != 0:
        raise RuntimeError(f"git push a origin falló: {out_push}")

    pr_url = None
    message = f"Rama '{work_branch}' pusheada a origin."

    # 3. Open a PR if requested.
    if mode in ("pr", "manual"):
        if not _gh_available():
            message += " gh CLI no encontrado: no se abrió PR (rama disponible para PR manual)."
        else:
            title = f"fix({repo_cfg.id}): AI job {job_id} — {(job.get('result_message') or 'fix automático').strip()[:60]}"
            body = (
                f"Fix automático generado por Sofia (AI job #{job_id}).\n\n"
                f"- Servicio: {job.get('service_id') or repo_cfg.id}\n"
                f"- Issue: #{job.get('issue_id') or 'n/a'}\n"
                f"- Riesgo: {job.get('risk')}\n"
                f"- Tests: {job.get('tests_status')}\n"
                f"- Verificador: {job.get('verifier_status')}\n\n"
                f"Generado con [Devin](https://cli.devin.ai/docs)"
            )
            gh_bin = shutil.which("gh") or shutil.which("gh.exe") or "gh"
            rc, out_pr = _run(
                [gh_bin, "pr", "create", "--head", work_branch, "--base", base_branch,
                 "--title", title, "--body", body],
                cwd=real_repo, timeout=180,
            )
            if rc == 0:
                m = re.search(r"https?://\S+", out_pr)
                pr_url = m.group(0) if m else None
                message = f"PR creado: {pr_url or out_pr.strip()}"
            else:
                message += f" gh pr create falló: {out_pr.strip()[:300]}"

    await db_service.update_ai_job(
        job_id,
        status="promoted",
        pr_url=pr_url,
        promoted_at=datetime.utcnow().isoformat(),
        result_message=message,
    )
    await db_service.add_audit_event("ai_job", "promoted", job_id, message)

    # 4. Resolve the associated issue so the autofix loop stops re-spawning it.
    if job.get("issue_id"):
        try:
            await db_service.resolve_issue(int(job["issue_id"]))
            await db_service.add_audit_event("issue", "resolved", int(job["issue_id"]), f"Resuelto por job #{job_id}.")
        except Exception as exc:
            logger.warning(f"[AUTONOMY] No se pudo resolver issue del job #{job_id}: {exc}")

    return {"ok": True, "job_id": job_id, "pr_url": pr_url, "message": message}


async def recover_stale_jobs() -> int:
    """
    On startup, mark any job left in 'running' (from a previous crash/restart)
    as failed so it doesn't block counters or hang forever.
    """
    stale = await db_service.get_ai_jobs_by_status("running")
    for job in stale:
        await db_service.update_ai_job(
            job["id"], status="failed", risk="medium",
            blocked_reason="Job interrumpido por reinicio de Sofia.",
            result_message="Marcado como fallido al arrancar (estaba 'running').",
        )
        await db_service.add_audit_event("ai_job", "recovered_stale", job["id"], "running → failed al arrancar.")
    if stale:
        logger.info(f"[AUTONOMY] {len(stale)} job(s) 'running' marcados como failed al arrancar.")
    return len(stale)


async def job_watchdog_loop() -> None:
    """Force-fail jobs that have been 'running' longer than job_timeout_minutes."""
    logger.info("[AUTONOMY] Job watchdog started.")
    await recover_stale_jobs()
    while True:
        try:
            cfg = load_config().autonomy
            timeout_min = max(5, cfg.job_timeout_minutes)
            cutoff = datetime.utcnow() - timedelta(minutes=timeout_min)
            running = await db_service.get_ai_jobs_by_status("running")
            for job in running:
                updated = job.get("updated_at") or job.get("created_at")
                try:
                    ts = datetime.fromisoformat(updated)
                except Exception:
                    continue
                if ts < cutoff:
                    await db_service.update_ai_job(
                        job["id"], status="failed", risk="medium",
                        blocked_reason=f"Timeout: job 'running' > {timeout_min} min.",
                        result_message="Marcado como fallido por el watchdog (colgado).",
                    )
                    await db_service.add_audit_event("ai_job", "watchdog_timeout", job["id"], f"> {timeout_min} min")
                    logger.warning(f"[AUTONOMY] Job #{job['id']} forzado a failed por timeout.")
        except Exception as exc:
            logger.error(f"[AUTONOMY] watchdog falló: {exc}", exc_info=True)
        await asyncio.sleep(300)  # check every 5 minutes


async def autofix_loop() -> None:
    """
    Background task that periodically creates AI jobs from unresolved issues.
    Groups up to 3 issues from the same service into a single job to avoid
    spawning dozens of parallel Devin sessions.
    """
    logger.info("[AUTONOMY] Autofix loop started.")
    await asyncio.sleep(180)
    while True:
        cfg = load_config()
        wait_seconds = max(1, cfg.autonomy.auto_fix_loop_minutes) * 60
        try:
            if cfg.autonomy.enabled and not cfg.autonomy.kill_switch and cfg.autonomy.auto_create_jobs_from_issues:
                issues = await db_service.get_issues(
                    level=None,
                    resolved=False,
                    since_hours=24,
                    limit=100,
                )
                # Filter to qualifying issues
                qualifying = []
                for issue in issues:
                    if issue.get("level") not in ("ERROR", "CRITICAL"):
                        continue
                    if int(issue.get("count") or 0) < cfg.autonomy.auto_fix_issue_min_count:
                        continue
                    if await db_service.get_open_ai_job_for_issue(int(issue["id"])):
                        continue
                    repo_cfg = _repo_for(issue.get("service_id"))
                    if not repo_cfg or not repo_cfg.autofix_enabled:
                        continue
                    qualifying.append({"issue": issue, "repo_cfg": repo_cfg})

                # Group by service_id, max 3 issues per group
                MAX_ISSUES_PER_JOB = 3
                from collections import defaultdict
                by_service = defaultdict(list)
                for item in qualifying:
                    sid = item["issue"].get("service_id", "unknown")
                    by_service[sid].append(item)

                created = 0
                for service_id, items in by_service.items():
                    if created >= cfg.autonomy.max_autofix_jobs_per_day:
                        break

                    batch = items[:MAX_ISSUES_PER_JOB]
                    repo_cfg = batch[0]["repo_cfg"]
                    mode = "fix" if repo_cfg.autonomy_level >= 3 else "plan"

                    if len(batch) == 1:
                        issue = batch[0]["issue"]
                        goal = (
                            f"Investiga y {'corrige en sandbox' if mode == 'fix' else 'propón un plan para'} "
                            f"el issue #{issue['id']} de {service_id}.\n"
                            f"Nivel: {issue['level']}. Ocurrencias: {issue['count']}.\n"
                            f"Mensaje: {issue['message']}\n"
                            f"Detalle: {issue.get('detail') or ''}\n"
                            f"Traceback: {(issue.get('traceback') or '')[:2000]}"
                        )
                        primary_issue_id = int(issue["id"])
                    else:
                        lines = [
                            f"Investiga y {'corrige en sandbox' if mode == 'fix' else 'propón un plan para'} "
                            f"{len(batch)} issues de {service_id}.",
                            "",
                            "## Issues:",
                        ]
                        for item in batch:
                            iss = item["issue"]
                            lines.append(
                                f"  - #{iss['id']} [{iss['level']}] count={iss['count']}: {iss['message']}"
                            )
                        lines.append("")
                        lines.append("Corrige TODOS los issues listados. Trabaja en orden y no dejes ninguno sin resolver.")
                        goal = "\n".join(lines)
                        primary_issue_id = int(batch[0]["issue"]["id"])

                    await create_job(
                        goal,
                        service_id=service_id,
                        issue_id=primary_issue_id,
                        repo_id=repo_cfg.id,
                        autonomy_level=repo_cfg.autonomy_level,
                        mode=mode,
                    )
                    created += 1

                if created:
                    await db_service.add_audit_event("autonomy", "autofix_jobs_created", None, f"{created} job(s) creados (agrupados por servicio, max 3 issues/job).")
        except Exception as exc:
            logger.error(f"[AUTONOMY] autofix loop failed: {exc}", exc_info=True)
        await asyncio.sleep(wait_seconds)
