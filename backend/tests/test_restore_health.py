"""Tests for restore_service health-check semantics (mocked HTTP, no real calls)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import restore_service


def _install_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        restore_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(transport=transport, timeout=5),
    )
    return transport


def _handler(routes):
    def handle(request):
        return routes[request.url.path]
    return handle


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_unexpected_status_not_healthy(monkeypatch, status):
    _install_transport(monkeypatch, lambda req: httpx.Response(status))
    ok = asyncio.run(restore_service._health_url_ok("http://x:1/api/ping"))
    assert ok is False


def test_explicit_expected_status_true(monkeypatch):
    _install_transport(monkeypatch, lambda req: httpx.Response(404))
    ok = asyncio.run(
        restore_service._health_url_ok("http://x:1/api/ingest", expected_status=404)
    )
    assert ok is True


def test_health_falls_back_to_healthz_on_404(monkeypatch):
    routes = {"/health": httpx.Response(404), "/healthz": httpx.Response(200)}
    _install_transport(monkeypatch, _handler(routes))
    ok = asyncio.run(restore_service._health_url_ok("http://x:1/health"))
    assert ok is True


def test_health_fallback_failure_not_healthy(monkeypatch):
    routes = {"/health": httpx.Response(404), "/healthz": httpx.Response(500)}
    _install_transport(monkeypatch, _handler(routes))
    ok = asyncio.run(restore_service._health_url_ok("http://x:1/health"))
    assert ok is False


def test_wpp_uses_auth_headers_and_semantic_status(monkeypatch):
    headers_seen = {}
    auth = AsyncMock(return_value={"Authorization": "Bearer tok"})

    def handle(request):
        headers_seen.update(dict(request.headers))
        return httpx.Response(200, json={"status": True})

    _install_transport(monkeypatch, handle)
    monkeypatch.setattr(restore_service, "load_config",
                        lambda: SimpleNamespace(alerts=object()))
    import app.services.whatsapp_service as wpp
    monkeypatch.setattr(wpp, "get_auth_headers", auth)

    ok = asyncio.run(
        restore_service._health_url_ok("http://localhost:21465/api/default/status-session")
    )
    assert ok is True
    auth.assert_awaited_once()
    assert headers_seen.get("authorization") == "Bearer tok"


def test_wpp_disconnected_200_not_healthy(monkeypatch):
    _install_transport(
        monkeypatch, lambda req: httpx.Response(200, json={"status": "DISCONNECTED"})
    )
    monkeypatch.setattr(restore_service, "load_config",
                        lambda: SimpleNamespace(alerts=object()))
    import app.services.whatsapp_service as wpp
    monkeypatch.setattr(wpp, "get_auth_headers", AsyncMock(return_value={}))
    ok = asyncio.run(
        restore_service._health_url_ok("http://localhost:21465/api/default/status-session")
    )
    assert ok is False


def test_wpp_invalid_json_not_healthy(monkeypatch):
    _install_transport(
        monkeypatch, lambda req: httpx.Response(200, content=b"not-json")
    )
    monkeypatch.setattr(restore_service, "load_config",
                        lambda: SimpleNamespace(alerts=object()))
    import app.services.whatsapp_service as wpp
    monkeypatch.setattr(wpp, "get_auth_headers", AsyncMock(return_value={}))
    ok = asyncio.run(
        restore_service._health_url_ok("http://localhost:21465/api/default/status-session")
    )
    assert ok is False


def test_check_diapi_mayor_targets_9000(monkeypatch):
    seen = {}

    def handle(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"connected": True})

    _install_transport(monkeypatch, handle)
    ok = asyncio.run(restore_service._check_diapi("mayor"))
    assert ok is True
    assert seen["url"] == "http://localhost:9000/api/Health/Ping"


def test_check_diapi_packing_targets_8001_health_index(monkeypatch):
    seen = {}

    def handle(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"connected": True})

    _install_transport(monkeypatch, handle)
    ok = asyncio.run(restore_service._check_diapi("packing"))
    assert ok is True
    assert seen["url"] == "http://localhost:8001/Health/Index"


def test_check_diapi_explicit_disconnected_false(monkeypatch):
    _install_transport(
        monkeypatch, lambda req: httpx.Response(200, json={"connected": False})
    )
    ok = asyncio.run(restore_service._check_diapi("mayor"))
    assert ok is False


def test_check_diapi_packing_requires_connected_true(monkeypatch):
    _install_transport(
        monkeypatch, lambda req: httpx.Response(200, json={"connected": True})
    )
    assert asyncio.run(restore_service._check_diapi("packing")) is True


@pytest.mark.parametrize(
    "body",
    [
        b"<html>OK</html>",
        b"not-json",
        b'{"connected": false}',
        b'{"connected": "false"}',
        b'{"foo": 1}',
    ],
)
def test_check_diapi_packing_rejects_bad_or_missing_connected(monkeypatch, body):
    _install_transport(
        monkeypatch, lambda req: httpx.Response(200, content=body)
    )
    assert asyncio.run(restore_service._check_diapi("packing")) is False


def test_check_diapi_mayor_keeps_200_liveness_without_connected(monkeypatch):
    _install_transport(
        monkeypatch, lambda req: httpx.Response(200, content=b"pong")
    )
    assert asyncio.run(restore_service._check_diapi("mayor")) is True


def test_check_diapi_unknown_service_false(monkeypatch):
    _install_transport(monkeypatch, lambda req: httpx.Response(200))
    ok = asyncio.run(restore_service._check_diapi("nope"))
    assert ok is False


def test_check_diapi_non_200_false(monkeypatch):
    _install_transport(monkeypatch, lambda req: httpx.Response(500))
    assert asyncio.run(restore_service._check_diapi("mayor")) is False


def test_packing_prompt_mentions_own_diapi_8001():
    extra = restore_service.SERVICE_CONTEXT["packing"]["extra"]
    assert extra == (
        "Packing manages its own SAP DI API service on port 8001. After Packing is up, "
        "verify http://localhost:8001/Health/Index returns HTTP 200. "
        "Do not restart Mayor or assume its port 9000 validates Packing."
    )


def test_wait_for_existing_restart_honors_expected_404(monkeypatch):
    svc = SimpleNamespace(id="ingest", url="http://x:1/api/ingest", expected_status=404)
    monkeypatch.setattr(
        restore_service, "load_config", lambda: SimpleNamespace(services=[svc])
    )
    _install_transport(monkeypatch, lambda req: httpx.Response(404))
    ok = asyncio.run(restore_service._wait_for_existing_restart("ingest", "Ingest"))
    assert ok is True
