"""Tests for codex_cli_service timeout/resolution behavior.

All run_codex tests mock build_codex_cmd and codex_available so the only
process ever launched is sys.executable running tiny inline scripts.
"""
import subprocess
import sys
import threading
import time
from unittest import mock

import psutil
import pytest

from app.services import codex_cli_service as svc

HUNG_AFTER_OUTPUT = 'import sys,time; print("ready",flush=True); time.sleep(30)'
HUNG_NO_READ = 'import time; time.sleep(30)'
ECHO_STDIN = 'import sys; print(sys.stdin.read())'
FAIL_EXIT7 = 'import sys; print("failed"); sys.exit(7)'
SPAWN_GRANDCHILD = (
    "import subprocess,sys,time; "
    "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
    " stdout=sys.stdout, stderr=sys.stdout); "
    "print(g.pid,flush=True); time.sleep(30)"
)


def test_incident_commands_never_request_full_access(monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "codex_bin", lambda: "codex-fixture")
    for readonly, expected in ((True, "read-only"), (False, "workspace-write")):
        command = svc.build_codex_cmd(readonly, str(tmp_path), safe_workspace=True)
        assert command[command.index("-s")+1] == expected
        assert "danger-full-access" not in command
        assert "sandbox_workspace_write.network_access=false" in command


def test_isolated_runner_strips_tokens_and_does_not_log_output(codex_env, monkeypatch, caplog):
    procs, set_cmd = codex_env
    monkeypatch.setenv("SOFIA_API_KEY", "operator-test-only")
    monkeypatch.setenv("GITHUB_TOKEN", "github-test-only")
    script = 'import os; print("private-output"); print(os.getenv("SOFIA_API_KEY","absent")); print(os.getenv("GITHUB_TOKEN","absent"))'
    m1, m2 = set_cmd([sys.executable, "-c", script])
    with m1, m2, caplog.at_level("INFO"):
        rc, output = svc.run_codex("fixture", True, safe_workspace=True, log_output=False)
    assert rc == 0 and output.count("absent") == 2
    assert "operator-test-only" not in output and "github-test-only" not in output
    assert "private-output" not in caplog.text


def test_output_without_newline_is_bounded(codex_env):
    procs, set_cmd = codex_env
    m1, m2 = set_cmd([sys.executable, "-c", 'import sys; sys.stdout.write("x"*200000); sys.stdout.flush()'])
    with m1, m2:
        rc, output = svc.run_codex("fixture", True, max_output_chars=1000, log_output=False, timeout=5)
    assert rc != 0 and len(output) < 1500
    assert procs[0].poll() is not None


def test_final_message_channel_ignores_cli_banner(codex_env, tmp_path, monkeypatch):
    procs, set_cmd = codex_env
    monkeypatch.setattr(svc.tempfile, "tempdir", str(tmp_path))
    script = 'import sys,pathlib; print("banner"); pathlib.Path(sys.argv[sys.argv.index("--output-last-message")+1]).write_text("{\\\"approved\\\":true}")'
    m1, m2 = set_cmd([sys.executable, "-c", script, "-"])
    with m1, m2:
        rc, output = svc.run_codex("fixture", True, final_message_only=True, log_output=False)
    assert rc == 0 and output == '{"approved":true}'
    assert not list(tmp_path.glob("sofia-codex-*.txt"))


@pytest.fixture
def tracked_popen():
    """Wrap subprocess.Popen to record every spawned process."""
    real_popen = subprocess.Popen
    procs = []
    def tracking(*args, **kwargs):
        p = real_popen(*args, **kwargs)
        procs.append(p)
        return p
    with mock.patch.object(svc.subprocess, "Popen", side_effect=tracking):
        yield procs


@pytest.fixture
def codex_env(tracked_popen):
    """Make run_codex use a given command while appearing available."""
    procs = tracked_popen
    def set_cmd(argv):
        return mock.patch.object(svc, "build_codex_cmd", return_value=argv), \
               mock.patch.object(svc, "codex_available", return_value=True)
    yield procs, set_cmd


def _run_with_guard(procs, prompt, timeout):
    """Call run_codex in a thread; fail (and kill children) if it never returns."""
    result = {}
    def call():
        result["v"] = svc.run_codex(prompt, read_only=True, timeout=timeout)
    t = threading.Thread(target=call, daemon=True)
    start = time.monotonic()
    t.start()
    t.join(15)
    elapsed = time.monotonic() - start
    if t.is_alive():
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        t.join(5)
        pytest.fail("run_codex did not return within 15s (timeout not enforced)")
    return result["v"], elapsed


def test_hung_stdout_timeout(codex_env):
    procs, set_cmd = codex_env
    m1, m2 = set_cmd([sys.executable, "-c", HUNG_AFTER_OUTPUT])
    with m1, m2:
        (rc, output), elapsed = _run_with_guard(procs, "prompt", timeout=1)
    assert rc == -1
    assert "ready" in output
    assert "TIMEOUT" in output
    assert elapsed < 4
    assert procs and procs[0].poll() is not None


def test_hung_stdin_write_timeout(codex_env):
    procs, set_cmd = codex_env
    m1, m2 = set_cmd([sys.executable, "-c", HUNG_NO_READ])
    with m1, m2:
        (rc, output), elapsed = _run_with_guard(procs, "x" * 1000000, timeout=1)
    assert rc == -1
    assert "TIMEOUT" in output
    assert elapsed < 4
    assert procs and procs[0].poll() is not None


def test_echo_success(codex_env):
    procs, set_cmd = codex_env
    m1, m2 = set_cmd([sys.executable, "-c", ECHO_STDIN])
    with m1, m2:
        rc, output = svc.run_codex("hello echo prompt", read_only=True, timeout=10)
    assert rc == 0
    assert "hello echo prompt" in output


def test_nonzero_exit(codex_env):
    procs, set_cmd = codex_env
    m1, m2 = set_cmd([sys.executable, "-c", FAIL_EXIT7])
    with m1, m2:
        rc, output = svc.run_codex("prompt", read_only=True, timeout=10)
    assert rc == 7
    assert "failed" in output


def test_early_exit_big_prompt_no_broken_pipe_mask(codex_env):
    procs, set_cmd = codex_env
    m1, m2 = set_cmd([sys.executable, "-c", FAIL_EXIT7])
    with m1, m2:
        rc, output = svc.run_codex("x" * 1000000, read_only=True, timeout=10)
    assert rc == 7
    assert "failed" in output


def test_kill_oserror_race_still_returns_timeout(codex_env):
    procs, set_cmd = codex_env
    real_popen = subprocess.Popen
    cleanup = []
    def tracking_kill_broken(*args, **kwargs):
        p = real_popen(*args, **kwargs)
        cleanup.append((p, p.kill))
        p.kill = mock.Mock(side_effect=OSError("process already gone"))
        procs.append(p)
        return p
    m1, m2 = set_cmd([sys.executable, "-c", HUNG_NO_READ])
    try:
        with m1, m2, mock.patch.object(svc.subprocess, "Popen", side_effect=tracking_kill_broken):
            rc, output = svc.run_codex("prompt", read_only=True, timeout=1)
        assert rc == -1
        assert "TIMEOUT" in output
    finally:
        # subprocess.Popen is patched by the fixture. Keep the real bound kill
        # before replacing it, and clean up only the process created by this test.
        for proc, kill in cleanup:
            if proc.poll() is None:
                kill()
            proc.wait(timeout=5)


def test_grandchild_with_inherited_stdout_reaped(codex_env):
    procs, set_cmd = codex_env
    m1, m2 = set_cmd([sys.executable, "-c", SPAWN_GRANDCHILD])
    with m1, m2:
        (rc, output), elapsed = _run_with_guard(procs, "prompt", timeout=1)
    assert rc == -1
    assert "TIMEOUT" in output
    assert elapsed < 4
    grandchild_pid = int(output.split("TIMEOUT")[0].strip().splitlines()[0])
    assert not psutil.pid_exists(grandchild_pid)
    assert procs and procs[0].poll() is not None


def test_popen_oserror():
    with mock.patch.object(svc, "codex_available", return_value=True), \
         mock.patch.object(svc, "build_codex_cmd", return_value=["x"]), \
         mock.patch.object(svc.subprocess, "Popen", side_effect=OSError("boom")):
        rc, output = svc.run_codex("prompt", read_only=True, timeout=10)
    assert rc == -3
    assert "ERROR" in output


def test_no_binary_no_popen(tracked_popen):
    with mock.patch.object(svc, "codex_available", return_value=False):
        rc, output = svc.run_codex("prompt", read_only=True, timeout=10)
    assert rc == -2
    assert tracked_popen == []


def test_resolver_env_override_valid(tmp_path, monkeypatch):
    fake = tmp_path / "codex-custom.exe"
    fake.write_bytes(b"MZ")
    fake.chmod(0o700)  # Resolver metadata fixture; no fake binary is executed.
    monkeypatch.setenv("SOFIA_CODEX_BIN", str(fake))
    assert svc.codex_bin() == str(fake)
    assert svc.codex_available() is True


def test_resolver_env_override_invalid(monkeypatch):
    monkeypatch.setenv("SOFIA_CODEX_BIN", "relative\\codex.exe")
    with mock.patch.object(svc.shutil, "which", return_value="C:\\tools\\codex.exe"):
        assert svc.codex_bin() is None
        assert svc.codex_available() is False
    monkeypatch.setenv("SOFIA_CODEX_BIN", "C:\\nonexistent\\codex.exe")
    with mock.patch.object(svc.shutil, "which", return_value="C:\\tools\\codex.exe"):
        assert svc.codex_bin() is None


def test_resolver_absent_uses_path(monkeypatch):
    monkeypatch.delenv("SOFIA_CODEX_BIN", raising=False)
    with mock.patch.object(svc.shutil, "which", side_effect=lambda n: "C:\\tools\\codex.exe" if n == "codex" else None):
        assert svc.codex_bin() == "C:\\tools\\codex.exe"


def test_command_flags_unchanged(tmp_path, monkeypatch):
    fake = tmp_path / "codex.exe"
    fake.write_bytes(b"MZ")
    fake.chmod(0o700)
    monkeypatch.setenv("SOFIA_CODEX_BIN", str(fake))
    monkeypatch.delenv("SOFIA_CODEX_MODEL", raising=False)
    cmd = svc.build_codex_cmd(read_only=True, cwd=str(tmp_path))
    assert cmd[:2] == [str(fake), "exec"]
    assert "--skip-git-repo-check" in cmd
    assert "--ephemeral" in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert "-m" not in cmd
    assert cmd[-1] == "-"
    cmd2 = svc.build_codex_cmd(read_only=False, cwd=str(tmp_path))
    assert cmd2[cmd2.index("-s") + 1] == "danger-full-access"
    monkeypatch.setenv("SOFIA_CODEX_MODEL", "test-model")
    cmd3 = svc.build_codex_cmd(read_only=True, cwd=str(tmp_path))
    assert cmd3[cmd3.index("-m") + 1] == "test-model"
