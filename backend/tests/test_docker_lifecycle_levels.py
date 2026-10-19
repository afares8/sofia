"""Severity of Docker lifecycle events (start/stop/die) reported by the droplet monitor."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services import droplet_docker_service as svc  # noqa: E402


def _reported_level(monkeypatch, action, exit_code, name="busy_hofstadter"):
    store = AsyncMock()
    monkeypatch.setattr(svc, "_store_and_alert", store)
    monkeypatch.setattr(svc, "_mark_seen", lambda *_: False)
    asyncio.run(svc._ingest_lifecycle(action, name, exit_code))
    if not store.await_count:
        return None
    return store.await_args.args[0].level


def test_start_is_info_not_warning(monkeypatch):
    assert _reported_level(monkeypatch, "start", None) == "INFO"


def test_stop_and_restart_stay_warning(monkeypatch):
    assert _reported_level(monkeypatch, "stop", 0) == "WARNING"
    assert _reported_level(monkeypatch, "restart", None) == "WARNING"


def test_die_without_exit_code_is_warning_not_error(monkeypatch):
    assert _reported_level(monkeypatch, "die", None) == "WARNING"


def test_die_with_nonzero_exit_stays_error(monkeypatch):
    assert _reported_level(monkeypatch, "die", 1) == "ERROR"


def test_die_clean_exit_is_warning(monkeypatch):
    assert _reported_level(monkeypatch, "die", 0) == "WARNING"
