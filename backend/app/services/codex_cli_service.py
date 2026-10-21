"""Shared Codex CLI runner for Sofia automation."""
from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import tempfile
from pathlib import Path
from typing import Optional

import psutil

logger = logging.getLogger("sofia.codex")

CODEX_NOT_FOUND = "CODEX_NOT_FOUND"
CODEX_MODEL_ENV = "SOFIA_CODEX_MODEL"
# Default to None so we don't pass -m and let Codex use the model from
# ~/.codex/config.toml (currently gpt-5.5).  Passing an unsupported model
# (e.g. "glm-5.2") causes Codex to fail with HTTP 400.
DEFAULT_CODEX_MODEL = ""


CODEX_BIN_ENV = "SOFIA_CODEX_BIN"


def _resolve_codex_bin() -> Optional[str]:
    override = os.getenv(CODEX_BIN_ENV, "").strip()
    if override:
        candidate = Path(override)
        if (
            candidate.is_absolute()
            and candidate.is_file()
            and os.access(str(candidate), os.X_OK)
        ):
            return str(candidate)
        return None
    return shutil.which("codex") or shutil.which("codex.exe")


def codex_available() -> bool:
    return _resolve_codex_bin() is not None


def codex_bin() -> Optional[str]:
    return _resolve_codex_bin()


def codex_model() -> str:
    return os.getenv(CODEX_MODEL_ENV, DEFAULT_CODEX_MODEL).strip()


def build_codex_cmd(read_only: bool, cwd: Optional[str] = None, *, safe_workspace: bool = False) -> list[str]:
    bin_path = codex_bin()
    if not bin_path:
        raise FileNotFoundError(CODEX_NOT_FOUND)

    work_dir = str(Path(cwd).expanduser()) if cwd else os.getcwd()
    sandbox = "read-only" if read_only else ("workspace-write" if safe_workspace else "danger-full-access")
    # NOTE: The old `-a never` (approval-policy) flag was removed from
    # `codex exec` in a recent version.  In non-interactive `exec` mode
    # approvals are not prompted, so the flag is no longer needed.
    cmd: list[str] = [
        bin_path,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "-C",
        work_dir,
        "-s",
        sandbox,
    ]
    # Only pass -m if an explicit model override is configured.
    # Otherwise let Codex use the model from ~/.codex/config.toml.
    model = codex_model()
    if model:
        cmd.extend(["-m", model])
    if safe_workspace:
        cmd.extend(["--ignore-user-config", "-c", 'approval_policy="never"',
                    "-c", "sandbox_workspace_write.writable_roots=[]",
                    "-c", "sandbox_workspace_write.network_access=false",
                    "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                    "-c", "sandbox_workspace_write.exclude_slash_tmp=true"])
    cmd.append("-")
    return cmd


def run_codex(
    prompt: str,
    read_only: bool,
    cwd: Optional[str] = None,
    timeout: int = 900,
    log_prefix: str = "CODEX",
    *,
    safe_workspace: bool = False,
    log_output: bool = True,
    max_output_chars: int = 1000000,
    final_message_only: bool = False,
) -> tuple[int, str]:
    """
    Run Codex non-interactively and stream output to Sofia logs.

    Returns (returncode, combined_output). The prompt is passed through stdin so
    long prompts are not truncated by Windows command-line length limits.
    """
    if not codex_available():
        return -2, "Codex CLI no encontrado en PATH."

    collected: list[str] = []
    output_chars = 0
    stopped = threading.Event()
    proc = None
    result_path = None
    try:
        cmd = (build_codex_cmd(read_only=read_only, cwd=cwd, safe_workspace=True) if safe_workspace
               else build_codex_cmd(read_only=read_only, cwd=cwd))
        if final_message_only:
            fd, result_path = tempfile.mkstemp(prefix="sofia-codex-", suffix=".txt")
            os.close(fd)
            cmd[-1:-1] = ["--output-last-message", result_path]
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
        if safe_workspace:
            # An isolated worker's model auth lives in its own profile. Do not
            # inherit application DB, messaging, GitHub or production tokens.
            allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME",
                       "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "PATHEXT"}
            popen_kwargs["env"] = {k: v for k, v in os.environ.items() if k.upper() in allowed}

        proc = subprocess.Popen(cmd, **popen_kwargs)
        assert proc.stdin is not None
        assert proc.stdout is not None

        deadline = time.monotonic() + timeout
        events: "queue.Queue" = queue.Queue(maxsize=128)

        def emit(kind, payload):
            while not stopped.is_set():
                try:
                    events.put((kind, payload), timeout=0.1)
                    return
                except queue.Full:
                    continue

        def _write_stdin() -> None:
            try:
                proc.stdin.write(prompt)
            except BrokenPipeError:
                pass
            except Exception as exc:
                emit("stdin_error", exc)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        def _read_stdout() -> None:
            try:
                while not stopped.is_set():
                    line = proc.stdout.readline(4096)
                    if not line:
                        break
                    emit("line", line)
            except Exception as exc:
                emit("read_error", exc)
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                emit("eof", None)

        threading.Thread(target=_write_stdin, daemon=True).start()
        threading.Thread(target=_read_stdout, daemon=True).start()

        eof = False
        while not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                kind, payload = events.get(timeout=remaining)
            except queue.Empty:
                raise subprocess.TimeoutExpired(cmd, timeout)
            if kind == "line":
                output_chars += len(payload)
                if output_chars > max_output_chars:
                    raise ValueError("Codex output limit exceeded")
                line_stripped = payload.rstrip()
                collected.append(line_stripped)
                if log_output:
                    logger.info(f"[{log_prefix}] {line_stripped}")
            elif kind == "eof":
                eof = True
            else:
                raise payload

        proc.wait(timeout=max(deadline - time.monotonic(), 0.001))
        if final_message_only and proc.returncode == 0:
            with open(result_path, encoding="utf-8") as final_result:
                output = final_result.read(max_output_chars+1)
            if not output or len(output) > max_output_chars:
                raise ValueError("Missing or excessive final message")
            return 0, output
        return proc.returncode or 0, "\n".join(collected)
    except subprocess.TimeoutExpired:
        if proc:
            _terminate_proc_tree(proc, log_prefix)
        msg = f"TIMEOUT: Codex no terminó en {timeout}s."
        logger.error(f"[{log_prefix}] {msg}")
        return -1, "\n".join(collected + [msg])
    except Exception as exc:
        if proc:
            _terminate_proc_tree(proc, log_prefix)
        msg = "ERROR: Codex failed (isolated worker)." if safe_workspace else f"ERROR: Codex failed: {exc}"
        logger.error(f"[{log_prefix}] {msg}")
        return -3, "\n".join(collected + [msg])
    finally:
        stopped.set()
        if result_path:
            try:
                os.unlink(result_path)
            except OSError:
                pass


def _terminate_proc_tree(proc: subprocess.Popen, log_prefix: str) -> None:
    try:
        descendants = psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    except Exception as exc:
        logger.warning(f"[{log_prefix}] cleanup: cannot enumerate descendants: {exc}")
        descendants = []
    for child in descendants:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:
            logger.warning(f"[{log_prefix}] cleanup: terminate pid {child.pid} failed: {exc}")
    try:
        proc.kill()
    except OSError:
        pass
    except Exception as exc:
        logger.warning(f"[{log_prefix}] cleanup: kill failed: {exc}")
    try:
        proc.wait(timeout=2)
    except Exception as exc:
        logger.warning(f"[{log_prefix}] cleanup: reap failed: {exc}")
    if descendants:
        try:
            psutil.wait_procs(descendants, timeout=2)
        except Exception as exc:
            logger.warning(f"[{log_prefix}] cleanup: descendant reap failed: {exc}")
