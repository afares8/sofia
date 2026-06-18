"""Shared Codex CLI runner for Sofia automation."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sofia.codex")

CODEX_NOT_FOUND = "CODEX_NOT_FOUND"
CODEX_MODEL_ENV = "SOFIA_CODEX_MODEL"
DEFAULT_CODEX_MODEL = "codex-5.3"


def codex_available() -> bool:
    return shutil.which("codex") is not None or shutil.which("codex.exe") is not None


def codex_bin() -> Optional[str]:
    return shutil.which("codex") or shutil.which("codex.exe")


def codex_model() -> str:
    return os.getenv(CODEX_MODEL_ENV, DEFAULT_CODEX_MODEL).strip() or DEFAULT_CODEX_MODEL


def build_codex_cmd(read_only: bool, cwd: Optional[str] = None) -> list[str]:
    bin_path = codex_bin()
    if not bin_path:
        raise FileNotFoundError(CODEX_NOT_FOUND)

    work_dir = str(Path(cwd).expanduser()) if cwd else os.getcwd()
    sandbox = "read-only" if read_only else "danger-full-access"
    return [
        bin_path,
        "exec",
        "-m",
        codex_model(),
        "--skip-git-repo-check",
        "--ephemeral",
        "-C",
        work_dir,
        "-s",
        sandbox,
        "-a",
        "never",
        "-",
    ]


def run_codex(
    prompt: str,
    read_only: bool,
    cwd: Optional[str] = None,
    timeout: int = 900,
    log_prefix: str = "CODEX",
) -> tuple[int, str]:
    """
    Run Codex non-interactively and stream output to Sofia logs.

    Returns (returncode, combined_output). The prompt is passed through stdin so
    long prompts are not truncated by Windows command-line length limits.
    """
    if not codex_available():
        return -2, "Codex CLI no encontrado en PATH."

    collected: list[str] = []
    proc = None
    try:
        cmd = build_codex_cmd(read_only=read_only, cwd=cwd)
        popen_kwargs = dict(
            cwd=cwd if cwd and Path(cwd).exists() else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        proc = subprocess.Popen(cmd, **popen_kwargs)
        assert proc.stdin is not None
        assert proc.stdout is not None

        proc.stdin.write(prompt)
        proc.stdin.close()

        for line in proc.stdout:
            line_stripped = line.rstrip()
            collected.append(line_stripped)
            logger.info(f"[{log_prefix}] {line_stripped}")

        proc.wait(timeout=timeout)
        return proc.returncode or 0, "\n".join(collected)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        msg = f"TIMEOUT: Codex no terminó en {timeout}s."
        logger.error(f"[{log_prefix}] {msg}")
        return -1, "\n".join(collected + [msg])
    except Exception as exc:
        msg = f"ERROR: Codex failed: {exc}"
        logger.error(f"[{log_prefix}] {msg}")
        return -3, "\n".join(collected + [msg])
