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


def test_proactor_accept_guard_keeps_listener_open_on_winerror_64():
    """Simula WinError 64 en f.result() de AcceptEx y verifica que el socket
    de escucha NO se cierre y vuelva a armar proactor.accept(sock)."""
    from asyncio import proactor_events as _pe
    from app.main import install_proactor_accept_guard

    assert install_proactor_accept_guard() is True

    class FakeSock:
        def __init__(self, fd=100):
            self._fd = fd
            self.closed = False

        def fileno(self):
            return -1 if self.closed else self._fd

        def close(self):
            self.closed = True

    class FakeFuture:
        def __init__(self, exc=None, res=None):
            self._exc = exc
            self._res = res
            self.cb = None

        def add_done_callback(self, cb):
            self.cb = cb

        def result(self):
            if self._exc is not None:
                raise self._exc
            return self._res

    sock = FakeSock(100)
    conn_sock = FakeSock(200)
    win64 = OSError(22, "The specified network name is no longer available", None, 64, None)
    f1 = FakeFuture(exc=win64)
    f2 = FakeFuture(res=(conn_sock, ("127.0.0.1", 55000)))
    f3 = FakeFuture()
    futures = [f1, f2, f3]
    accepted = []

    class DummyLoop:
        _debug = False
        _accept_futures = {}

        def is_closed(self):
            return False

        def call_soon(self, fn):
            self._entry = fn

        def _make_socket_transport(self, conn, protocol, extra=None, server=None):
            accepted.append((conn, extra))

        class _proactor:
            @staticmethod
            def accept(s):
                return futures.pop(0)

    loop_obj = DummyLoop()
    _pe.BaseProactorEventLoop._start_serving(loop_obj, lambda: object(), sock)
    loop_obj._entry()  # arma f1
    assert sock.closed is False
    assert f1.cb is not None

    # Dispara f1 con WinError 64: NO debe cerrar sock y debe armar f2
    f1.cb(f1)
    assert sock.closed is False
    assert f2.cb is not None

    # Dispara f2 con conexion sana: debe aceptar y armar f3
    f2.cb(f2)
    assert sock.closed is False
    assert len(accepted) == 1
    assert accepted[0][0] is conn_sock

