import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from app.services import db_service
from app.services.config_service import load_config

logger = logging.getLogger("sofia.github_sync")

# Only HIGH-CONFIDENCE real secrets — avoid false positives on code that
# merely uses variable names like `token`, `apiKey`, `password`.
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),
    re.compile(r"gho_[A-Za-z0-9]{36,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),                      # AWS access key
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),                # Google API key
    re.compile(r"sk-[A-Za-z0-9]{40,}"),                   # OpenAI-style key
    re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}"),         # Slack token
]

# Windows reserved device names that git reports but can never be staged.
WINDOWS_RESERVED_NAMES = {
    "nul", "con", "prn", "aux",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}


def _is_windows_reserved(path: str) -> bool:
    """True if the file's basename (without extension) is a Windows reserved name."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.split(".", 1)[0].lower()
    return stem in WINDOWS_RESERVED_NAMES


def _run(cmd: list[str], cwd: str, timeout: int = 120) -> tuple[int, str]:
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


def _status_files(repo_path: str) -> list[str]:
    rc, out = _run(["git", "status", "--porcelain"], repo_path)
    if rc != 0:
        raise RuntimeError(out)
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        norm = path.replace("\\", "/")
        # Skip Windows reserved device names (nul, con, etc.) — they are git
        # artifacts that can never be staged and would break the whole sync.
        if _is_windows_reserved(norm):
            logger.warning(f"[GITHUB_SYNC] Ignorando nombre reservado de Windows: {norm}")
            continue
        files.append(norm)
    return files


def _extract_sofia_modified_files(repo_id: str, since_hours: int = 24) -> list[str]:
    """
    Look at recent successful proposal runs and AI jobs for this repo
    and extract which files Devin actually modified, by parsing Devin output.
    """
    files: set[str] = set()
    try:
        import sqlite3
        from pathlib import Path
        db = Path(__file__).resolve().parents[2] / "data" / "sofia.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        # From proposal_runs (nightly review fixes)
        cur = conn.execute(
            "SELECT devin_output FROM proposal_runs "
            "WHERE service_id = ? AND success = 1 AND started_at >= datetime('now', '-{} hours')"
            .format(since_hours),
            (repo_id,)
        )
        for row in cur.fetchall():
            files.update(_parse_files_from_devin_output(row["devin_output"] or ""))

        # From ai_jobs (autofix loop)
        cur2 = conn.execute(
            "SELECT result_message FROM ai_jobs "
            "WHERE repo_id = ? AND status = 'completed' AND updated_at >= datetime('now', '-{} hours')"
            .format(since_hours),
            (repo_id,)
        )
        for row in cur2.fetchall():
            files.update(_parse_files_from_devin_output(row["result_message"] or ""))

        conn.close()
    except Exception as exc:
        logger.warning(f"[GITHUB_SYNC] Could not lookup Sofia-modified files: {exc}")
    return sorted(files)


def _parse_files_from_devin_output(text: str) -> set[str]:
    """Parse Devin output to find files it claims to have modified."""
    files: set[str] = set()
    if not text:
        return files

    # Pattern: "File modified: `path`"
    for m in re.finditer(r"[Ff]ile\s+(?:modified|edited|changed|created)\s*[:\-]?\s*[`\"']?([^`\"'\n]+)[`\"']?", text):
        f = m.group(1).strip().replace("\\", "/")
        if f and not f.startswith("http"):
            files.add(f)

    # Pattern: "**File:** `path`"
    for m in re.finditer(r"\*\*[Ff]ile:\*\*\s*[`\"']?([^`\"'\n]+)[`\"']?", text):
        f = m.group(1).strip().replace("\\", "/")
        if f and not f.startswith("http"):
            files.add(f)

    # Pattern: backtick paths with extensions like .py, .ts, etc.
    code_exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".sql", ".md", ".txt"}
    for m in re.finditer(r"`([^`]+\.(?:py|ts|tsx|js|jsx|json|yaml|yml|sql|md|txt))`", text, re.IGNORECASE):
        f = m.group(1).strip().replace("\\", "/")
        if f and not f.startswith("http") and ":" not in f:
            files.add(f)

    return files


def _is_safe_code_file(file_path: str) -> bool:
    """Check if a file is a safe code/config file that Sofia is allowed to sync."""
    path = file_path.lower().replace("\\", "/")
    safe_exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".sql", ".md", ".html", ".css", ".scss", ".txt"}
    if any(path.endswith(ext) for ext in safe_exts):
        return True
    # Also allow known config files without extension
    basename = Path(path).name
    safe_names = {"dockerfile", "makefile", ".gitignore", ".dockerignore", "readme"}
    if basename.lower() in safe_names:
        return True
    return False


def _blocked(file_path: str, blocked_paths: list[str]) -> bool:
    path = file_path.replace("\\", "/")
    for p in blocked_paths:
        p_norm = p.strip("/")
        # Exact match
        if path == p_norm:
            return True
        # Directory prefix match (e.g. 'data/' matches 'data/foo.json')
        if path.startswith(p_norm + "/"):
            return True
        # Also match if the blocked path is a directory anywhere in the path
        # e.g. 'backend/data/' should match 'backend/data/foo.json'
        # but NOT match 'backend/app/data/foo.json' (different directory)
        parts = path.split("/")
        for i in range(len(parts)):
            prefix = "/".join(parts[:i+1])
            if prefix == p_norm:
                # The path starts with this blocked dir, check if it's a prefix
                remaining = path[len(prefix):]
                if remaining == "" or remaining.startswith("/"):
                    return True
    return False


def _secret_scan(repo_path: str, files: list[str]) -> list[str]:
    hits = []
    root = Path(repo_path)
    for rel in files:
        path = root / rel
        if not path.exists() or path.is_dir():
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if any(p.search(text) for p in SECRET_PATTERNS):
            hits.append(rel)
    return hits


def _devin_available() -> bool:
    return shutil.which("devin") is not None or shutil.which("devin.exe") is not None


def _build_conflict_prompt(repo_path: str, conflict_files: list[str], branch: str) -> str:
    """
    Build a prompt for Devin to resolve merge conflicts.

    THE FIXED RULE (siempre la misma orden):
      Montá los cambios locales sin que se pierda nada de la nube,
      y traé los cambios de la nube sin que se pierda nada local.
      NUNCA borres ni descartes un cambio — ni local ni remoto.
    """
    lines = [
        f"You are resolving a git merge/rebase conflict in the repository at {repo_path}.",
        f"The repo is on branch '{branch}'.",
        "",
        "## THE GOLDEN RULE — NEVER LOSE ANY CHANGE",
        "Your job is to integrate LOCAL changes and REMOTE (GitHub) changes so that:",
        "  - NOT A SINGLE local change is lost.",
        "  - NOT A SINGLE remote/GitHub change is lost.",
        "  - Both sides are preserved and merged intelligently.",
        "",
        "## Conflict files:",
    ]
    for f in conflict_files:
        lines.append(f"  - {f}")
    lines.append("")
    lines.append("## Rules:")
    lines.append("- For EACH conflict, KEEP BOTH the local change AND the remote change.")
    lines.append("- If local and remote edit the SAME line, merge them so both intentions survive.")
    lines.append("- If they edit DIFFERENT parts, keep all of them — there is no real conflict.")
    lines.append("- NEVER delete code, tests, docs, configuration, or data from either side.")
    lines.append("- NEVER pick one side and throw away the other unless they are byte-identical duplicates.")
    lines.append("- Remove ONLY the conflict markers (<<<<<<<, =======, >>>>>>>), never real content.")
    lines.append("- After resolving each file, run 'git add <file>' to stage it.")
    lines.append("- Do NOT commit and do NOT abort the rebase — just stage the resolved files.")
    lines.append("")
    lines.append("## Step-by-step:")
    lines.append("1. For each conflicted file, read it and understand BOTH sides fully.")
    lines.append("2. Edit the file so it contains BOTH the local and remote changes, merged.")
    lines.append("3. Remove the conflict markers, keeping all real content from both sides.")
    lines.append("4. Stage the resolved file with 'git add <file>'.")
    lines.append("5. Repeat for ALL conflicted files — do not skip any.")
    lines.append("6. Report which files you resolved and how you merged each one.")
    lines.append("")
    lines.append("Output a final summary line:")
    lines.append("  CONFLICT_RESOLVED: <comma-separated files>")
    return "\n".join(lines)


def _run_devin(prompt: str, cwd: str, timeout: int = 300) -> tuple[int, str]:
    """
    Run Devin CLI to resolve conflicts. Returns (rc, output).

    Output is streamed line-by-line to the Sofia logger in real time so you
    can watch what Devin is doing in the Sofia logs/UI instead of a black
    console window. No new console window is opened on Windows.
    """
    if not _devin_available():
        return -1, "Devin CLI not found."
    devin_bin = shutil.which("devin") or shutil.which("devin.exe") or "devin"
    tmp_path = None
    collected: list[str] = []
    proc = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(prompt)
            tmp_path = f.name
        cmd = [devin_bin, "--permission-mode", "dangerous", "--prompt-file", tmp_path, "--print"]

        popen_kwargs = dict(
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout
            stdin=subprocess.DEVNULL,   # no TTY — prevents console takeover
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # Windows: don't open a new black console window
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        proc = subprocess.Popen(cmd, **popen_kwargs)

        # Stream every line to the Sofia logger in real time
        assert proc.stdout is not None
        for line in proc.stdout:
            line_stripped = line.rstrip()
            collected.append(line_stripped)
            logger.info(f"[DEVIN] {line_stripped}")

        proc.wait(timeout=timeout)
        return proc.returncode, "\n".join(collected)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        msg = f"Devin timeout after {timeout}s"
        logger.error(f"[DEVIN] {msg}")
        return -1, "\n".join(collected) + "\n" + msg
    except Exception as exc:
        msg = f"Devin error: {exc}"
        logger.error(f"[DEVIN] {msg}")
        return -1, "\n".join(collected) + "\n" + msg
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def sync_repo(repo_cfg) -> dict:
    cfg = load_config().github_sync
    repo_path = str(Path(repo_cfg.path).expanduser())
    run_id = await db_service.create_github_sync_run(repo_cfg.id, repo_path, repo_cfg.branch)
    try:
        if not repo_cfg.enabled:
            await db_service.finish_github_sync_run(run_id, "skipped", output="Repo deshabilitado.")
            return {"repo_id": repo_cfg.id, "status": "skipped"}
        if not Path(repo_path, ".git").exists():
            raise RuntimeError(f"No es un repo git: {repo_path}")

        # ── PASO 0: Limpiar cualquier rebase/merge a medias de un sync anterior ──
        # Si un sync previo fue interrumpido (ej: backend reiniciado a mitad del
        # rebase), el repo queda en estado raro. Lo abortamos para volver a un
        # estado limpio SIN perder nada (los commits locales se conservan).
        git_dir = Path(repo_path, ".git")
        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            logger.warning(f"[GITHUB_SYNC] Rebase a medias detectado en {repo_cfg.id}. Abortando para limpiar estado.")
            _run(["git", "rebase", "--abort"], repo_path, timeout=60)
        if (git_dir / "MERGE_HEAD").exists():
            logger.warning(f"[GITHUB_SYNC] Merge a medias detectado en {repo_cfg.id}. Abortando para limpiar estado.")
            _run(["git", "merge", "--abort"], repo_path, timeout=60)

        rc, out = _run(["git", "branch", "--show-current"], repo_path)
        if rc != 0:
            raise RuntimeError(out)
        current_branch = out.strip()
        if repo_cfg.branch and current_branch != repo_cfg.branch:
            raise RuntimeError(f"Branch actual '{current_branch}' != branch permitido '{repo_cfg.branch}'")

        output_parts = []
        staged_files: list[str] = []

        all_files = _status_files(repo_path)

        # Filtrar archivos bloqueados (.env, secrets, data/, logs/) y con secretos.
        # NO abortamos por estos — solo los excluimos del commit.
        files = []
        excluded_blocked = []
        for f in all_files:
            if _blocked(f, cfg.blocked_paths):
                excluded_blocked.append(f)
            else:
                files.append(f)
        if excluded_blocked:
            output_parts.append(
                f"[SKIP] {len(excluded_blocked)} archivo(s) bloqueado(s) (no se commitean): "
                + ", ".join(excluded_blocked[:10])
            )

        if files and cfg.require_clean_secret_scan:
            hits = _secret_scan(repo_path, files)
            if hits:
                hit_set = set(hits)
                files = [f for f in files if f not in hit_set]
                output_parts.append(
                    f"[SKIP] {len(hits)} archivo(s) con posibles secretos (no se commitean): "
                    + ", ".join(hits[:10])
                )

        # ─────────────────────────────────────────────────────────────────────
        # ORDEN CORRECTO (lo que el usuario pidió):
        #   1. Montar cambios LOCALES primero (add + commit) → quedan seguros
        #   2. Traer cambios de la NUBE (pull/merge) → sin perder local
        #   3. Si hay conflicto → Devin mergea preservando AMBOS lados
        #   4. Push → sube el resultado completo
        # ─────────────────────────────────────────────────────────────────────

        # ── PASO 1 + 2: Montar y commitear cambios locales (SI hay archivos) ──
        if files:
            skipped_files: list[str] = []
            for f in files:
                rc, out = _run(["git", "add", "--", f], repo_path, timeout=60)
                if rc == 0:
                    staged_files.append(f)
                else:
                    skipped_files.append(f)
                    logger.warning(f"[GITHUB_SYNC] No se pudo stagear {f} en {repo_cfg.id}: {out[:100]}")

            if skipped_files:
                output_parts.append(
                    f"[STAGE] Saltados {len(skipped_files)} archivo(s) rotos: " + ", ".join(skipped_files[:10])
                )

            if staged_files:
                output_parts.append(f"[STAGE] {len(staged_files)} archivo(s) montados.")
                msg = f"{cfg.commit_message_prefix} ({datetime.now().strftime('%Y-%m-%d')})"
                commit_body = (
                    msg
                    + "\n\nGenerated with [Devin](https://cli.devin.ai/docs)\n\n"
                    + "Co-Authored-By: Devin <158243242+devin-ai-integration[bot]@users.noreply.github.com>\n"
                )
                rc, out = _run(["git", "commit", "-m", commit_body], repo_path, timeout=180)
                output_parts.append(f"[COMMIT] {out}")
                if rc != 0 and "nothing to commit" not in out.lower():
                    raise RuntimeError(f"git commit falló: {out}")

        # ── PASO 3: Fetch remoto ──
        rc, out = _run(["git", "fetch", "origin", current_branch], repo_path, timeout=180)
        output_parts.append(f"[FETCH] {out}")
        if rc != 0:
            raise RuntimeError(f"git fetch falló: {out}")

        # ── PASO 4: Traer la nube SOLO si hay commits nuevos allá (behind > 0) ──
        # Contamos cuántos commits estamos adelante/atrás de la nube.
        #   ahead  = commits locales que la nube NO tiene → hay que pushear
        #   behind = commits de la nube que NOSOTROS no tenemos → hay que pullear
        # Solo hacemos pull --rebase si behind > 0. Si solo estamos adelante
        # (behind == 0), NO hay divergencia real: vamos directo al push.
        ahead, behind = 0, 0
        rc_cnt, out_cnt = _run(
            ["git", "rev-list", "--left-right", "--count", f"origin/{current_branch}...HEAD"],
            repo_path,
        )
        if rc_cnt == 0 and out_cnt.strip():
            parts = out_cnt.split()
            if len(parts) == 2:
                behind, ahead = int(parts[0]), int(parts[1])
        output_parts.append(f"[DIVERGENCE] ahead={ahead}, behind={behind}")

        if behind > 0:
            # La nube tiene commits que no tenemos → traerlos con rebase.
            # --autostash guarda temporalmente archivos sin commitear (PDFs, etc.).
            logger.info(f"[GITHUB_SYNC] {repo_cfg.id}: behind={behind}, trayendo cambios de la nube...")
            rc_rebase, out_rebase = _run(
                ["git", "pull", "--rebase", "--autostash", "origin", current_branch], repo_path, timeout=300
            )
            output_parts.append(f"[PULL --rebase --autostash] {out_rebase}")
            if rc_rebase != 0:
                # Rebase falló — revisar conflictos
                rc_conf, out_conf = _run(["git", "diff", "--name-only", "--diff-filter=U"], repo_path)
                if rc_conf == 0 and out_conf.strip():
                    conflict_files = [f.strip() for f in out_conf.strip().splitlines() if f.strip()]
                    logger.warning(
                        f"[GITHUB_SYNC] Conflictos en {repo_cfg.id}: {conflict_files}. Llamando a Devin..."
                    )
                    # Devin resuelve EN MEDIO del rebase (no abortamos — preservamos ambos)
                    prompt = _build_conflict_prompt(repo_path, conflict_files, current_branch)
                    devin_rc, devin_out = _run_devin(prompt, repo_path, timeout=480)
                    output_parts.append(f"[DEVIN] rc={devin_rc}\n{devin_out}")
                    if devin_rc != 0:
                        _run(["git", "rebase", "--abort"], repo_path, timeout=60)
                        raise RuntimeError(
                            f"Devin no pudo resolver conflictos. Archivos: {', '.join(conflict_files)}. "
                            f"Rebase abortado, nada se perdió. Output: {devin_out[:400]}"
                        )
                    # Verificar que no queden marcadores de conflicto
                    rc_v, out_v = _run(["git", "diff", "--name-only", "--diff-filter=U"], repo_path)
                    if rc_v == 0 and out_v.strip():
                        _run(["git", "rebase", "--abort"], repo_path, timeout=60)
                        raise RuntimeError(
                            f"Devin dijo resolver pero quedan conflictos: {out_v.strip()}. Rebase abortado."
                        )
                    # Continuar el rebase con los archivos resueltos por Devin
                    rc_cont, out_cont = _run(["git", "rebase", "--continue"], repo_path, timeout=180)
                    output_parts.append(f"[REBASE --continue] {out_cont}")
                    if rc_cont != 0:
                        _run(["git", "rebase", "--abort"], repo_path, timeout=60)
                        raise RuntimeError(f"git rebase --continue falló: {out_cont}. Abortado.")
                else:
                    raise RuntimeError(f"git pull --rebase falló sin conflictos detectables: {out_rebase}")
        else:
            # behind == 0: la nube no tiene nada nuevo. No hace falta pull.
            output_parts.append("[PULL] Sin commits nuevos en la nube (behind=0). Voy directo al push.")

        rc, sha_out = _run(["git", "rev-parse", "HEAD"], repo_path)
        commit_sha = sha_out.strip() if rc == 0 else None

        # ── PASO 5: Push — sube el resultado completo (local + nube mergeados) ──
        # Recontamos: si no hay nada que pushear (ahead==0 tras el pull), saltamos.
        rc_chk, out_chk = _run(
            ["git", "rev-list", "--count", f"origin/{current_branch}..HEAD"], repo_path
        )
        to_push = int(out_chk.strip()) if rc_chk == 0 and out_chk.strip().isdigit() else 1
        if to_push == 0:
            output_parts.append("[PUSH] Nada que pushear — ya está todo sincronizado.")
        else:
            rc, out = _run(["git", "push", "origin", current_branch], repo_path, timeout=300)
            output_parts.append(f"[PUSH] ({to_push} commit/s) {out}")
            if rc != 0:
                raise RuntimeError(f"git push falló: {out}")

        await db_service.finish_github_sync_run(
            run_id,
            "success",
            files_changed=len(staged_files),
            commit_sha=commit_sha,
            pushed=True,
            output="\n".join(output_parts),
        )
        return {"repo_id": repo_cfg.id, "status": "success", "files_changed": len(staged_files), "commit_sha": commit_sha}
    except Exception as exc:
        await db_service.finish_github_sync_run(run_id, "blocked", error_msg=str(exc))
        return {"repo_id": repo_cfg.id, "status": "blocked", "error": str(exc)}


async def sync_all_repos() -> list[dict]:
    cfg = load_config().github_sync
    if not cfg.enabled:
        return []
    results = []
    for repo in cfg.repos:
        results.append(await sync_repo(repo))
    return results


async def sync_all_repos_background() -> None:
    try:
        results = await sync_all_repos()
        logger.info(f"[GITHUB_SYNC] completed: {results}")
    except Exception as exc:
        logger.error(f"[GITHUB_SYNC] failed: {exc}", exc_info=True)
