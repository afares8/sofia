"""Tests for /health/sofia self-health endpoint (direct call, mocked deps)."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.routers import health as health_router
from app.services import codex_cli_service, log_service


def _base_mocks(monkeypatch):
    monkeypatch.setattr(
        health_router.db_service, "get_issues", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        health_router.db_service, "get_metrics", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        health_router, "load_config",
        lambda: SimpleNamespace(services=[], alerts=object()),
    )
    monkeypatch.setattr(
        health_router.whatsapp_service, "get_connection_status",
        AsyncMock(return_value={"status": True}),
    )


def test_memory_rss_reported_in_mb(monkeypatch):
    _base_mocks(monkeypatch)
    fake = SimpleNamespace(
        Process=lambda pid: SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=42 * 1024 * 1024)
        )
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    result = asyncio.run(health_router.sofia_self_health())
    assert result["memory_mb"] == 42.0


def test_memory_failure_returns_none(monkeypatch):
    _base_mocks(monkeypatch)

    def boom(pid):
        raise OSError("no proc")

    fake = SimpleNamespace(Process=boom)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    result = asyncio.run(health_router.sofia_self_health())
    assert result["memory_mb"] is None


def test_log_monitor_status_surfaced(monkeypatch):
    _base_mocks(monkeypatch)
    fake_status = {"last_poll": "t0", "sources": {"mayor": {"state": "ready"}}}
    monkeypatch.setattr(
        log_service, "get_monitor_status", lambda: fake_status
    )
    result = asyncio.run(health_router.sofia_self_health())
    assert result["log_monitor"] == fake_status


def test_codex_absent_reports_false(monkeypatch):
    _base_mocks(monkeypatch)
    monkeypatch.setattr(codex_cli_service, "codex_available", lambda: False)
    result = asyncio.run(health_router.sofia_self_health())
    assert result["automation"] == {"codex_available": False}


def test_ok_and_db_ok_semantics_preserved(monkeypatch):
    _base_mocks(monkeypatch)
    monkeypatch.setattr(
        health_router.db_service, "get_issues",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    result = asyncio.run(health_router.sofia_self_health())
    assert result["ok"] is False
    assert result["db_ok"] is False
    assert result["db_error"]
