import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.models.config import ServiceConfig
from app.services import health_service


@pytest.fixture(autouse=True)
def isolated_health(monkeypatch):
    monkeypatch.setattr(health_service, "_status_cache", {})
    monkeypatch.setattr(health_service.db_service, "record_metric", AsyncMock())
    monkeypatch.setattr(health_service.db_service, "insert_event", AsyncMock(return_value=1))
    monkeypatch.setattr(health_service.db_service, "mark_notified", AsyncMock())
    monkeypatch.setattr(health_service.whatsapp_service, "send_alert", AsyncMock(return_value=False))
    monkeypatch.setattr(health_service.whatsapp_service, "get_auth_headers", AsyncMock(return_value={"Authorization": "Bearer test"}))
    from app.services import restore_service
    monkeypatch.setattr(restore_service, "notify_down_with_restore_prompt", AsyncMock())
    monkeypatch.setattr(health_service, "load_config", lambda: SimpleNamespace(alerts=object(), poll_interval_seconds=30))


def mock_http(monkeypatch, responses):
    client = MagicMock()
    client.get = AsyncMock(side_effect=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(health_service.httpx, "AsyncClient", MagicMock(return_value=client))
    return client


@pytest.mark.parametrize("code", [301, 401, 403, 404, 429, 500])
def test_unexpected_status_is_not_healthy(monkeypatch, code):
    mock_http(monkeypatch, [httpx.Response(code)])
    status = asyncio.run(health_service.check_service(ServiceConfig(id="mayor", name="Mayor", url="http://localhost/ping")))
    assert status.status == "restarting"
    assert status.consecutive_failures == 1


def test_explicit_expected_404_is_healthy(monkeypatch):
    mock_http(monkeypatch, [httpx.Response(404)])
    status = asyncio.run(health_service.check_service(ServiceConfig(id="probe", name="Probe", url="http://localhost/ingest", expected_status=404)))
    assert status.status == "up"


def test_missing_health_fallback_is_not_healthy(monkeypatch):
    mock_http(monkeypatch, [httpx.Response(404), httpx.Response(404)])
    status = asyncio.run(health_service.check_service(ServiceConfig(id="packing", name="Packing", url="http://localhost/health")))
    assert status.status == "restarting"


def test_successful_health_fallback_is_healthy(monkeypatch):
    mock_http(monkeypatch, [httpx.Response(404), httpx.Response(200)])
    status = asyncio.run(health_service.check_service(ServiceConfig(id="packing", name="Packing", url="http://localhost/health")))
    assert status.status == "up"
    assert status.status_code == 200


def test_wpp_refreshes_rejected_token(monkeypatch):
    client = mock_http(monkeypatch, [httpx.Response(401), httpx.Response(200, json={"status": True})])
    status = asyncio.run(health_service.check_service(ServiceConfig(id="wppconnect", name="WPP", url="http://localhost:21465/api/default/check-connection-session")))
    assert status.status == "up"
    assert client.get.await_count == 2
    assert health_service.whatsapp_service.get_auth_headers.await_args_list[1].kwargs == {"force_refresh": True}


@pytest.mark.parametrize("payload", [{"status": False}, {"status": "Disconnected"}, {}])
def test_wpp_http_200_does_not_hide_disconnection(monkeypatch, payload):
    mock_http(monkeypatch, [httpx.Response(200, json=payload)])
    status = asyncio.run(health_service.check_service(ServiceConfig(id="wppconnect", name="WPP", url="http://localhost:21465/api/default/check-connection-session")))
    assert status.status == "restarting"


def test_alert_failure_does_not_lose_down_transition(monkeypatch):
    mock_http(monkeypatch, [httpx.Response(503), httpx.Response(503)])
    health_service.whatsapp_service.send_alert.side_effect = RuntimeError("notification unavailable")
    service = ServiceConfig(id="packing", name="Packing", url="http://localhost/ping", failure_threshold=1)

    async def run():
        for _ in range(2):
            try:
                await health_service.check_service(service)
            except RuntimeError:
                pass

    asyncio.run(run())
    assert health_service.get_status("packing").status == "down"
    assert health_service.get_status("packing").consecutive_failures == 2
    assert health_service.db_service.insert_event.await_count == 1
    assert health_service.db_service.record_metric.await_count == 2


def test_alerts_only_on_confirmed_outage_and_recovery(monkeypatch):
    codes = [503, 200, 503, 503, 503, 503, 200]
    mock_http(monkeypatch, [httpx.Response(code) for code in codes])
    service = ServiceConfig(id="mayor", name="Mayor", url="http://localhost/ping")

    async def run():
        return [(await health_service.check_service(service)).status for _ in codes]

    assert asyncio.run(run()) == ["restarting", "up", "restarting", "restarting", "down", "down", "up"]
    assert health_service.db_service.insert_event.await_count == 1
    assert health_service.whatsapp_service.send_alert.await_count == 2


def test_wppconnect_down_defers_fallback_email(monkeypatch):
    """WPPConnect caido: la alerta se intenta igual, pero el correo de respaldo
    queda diferido (si vuelve solo, no se manda correo)."""
    mock_http(monkeypatch, [httpx.Response(503)] * 3)
    service = ServiceConfig(
        id="wppconnect", name="WPP",
        url="http://localhost:21465/api/default/check-connection-session")

    async def run():
        for _ in range(3):
            await health_service.check_service(service)

    asyncio.run(run())
    assert health_service.whatsapp_service.send_alert.await_count == 1
    kwargs = health_service.whatsapp_service.send_alert.await_args.kwargs
    assert kwargs["defer_email_seconds"] == health_service.WPPCONNECT_EMAIL_DEFER_SECONDS


def test_other_service_down_does_not_defer_email(monkeypatch):
    mock_http(monkeypatch, [httpx.Response(503)] * 3)
    service = ServiceConfig(id="packing", name="Packing", url="http://localhost/ping")

    async def run():
        for _ in range(3):
            await health_service.check_service(service)

    asyncio.run(run())
    assert health_service.whatsapp_service.send_alert.await_count == 1
    kwargs = health_service.whatsapp_service.send_alert.await_args.kwargs
    assert kwargs["defer_email_seconds"] == 0


def test_services_are_polled_concurrently(monkeypatch):
    class StopPolling(Exception):
        pass

    services = [ServiceConfig(id=sid, name=sid, url="http://localhost/ping") for sid in ("wppconnect", "mayor")]
    monkeypatch.setattr(health_service, "load_config", lambda: SimpleNamespace(services=services, poll_interval_seconds=30))

    async def run():
        mayor_checked = asyncio.Event()
        completed = []

        async def check(service):
            if service.id == "wppconnect":
                await asyncio.wait_for(mayor_checked.wait(), timeout=0.1)
            else:
                mayor_checked.set()
            completed.append(service.id)

        monkeypatch.setattr(health_service, "check_service", check)
        monkeypatch.setattr(health_service.asyncio, "sleep", AsyncMock(side_effect=StopPolling))
        with pytest.raises(StopPolling):
            await health_service.poll_loop()
        assert sorted(completed) == ["mayor", "wppconnect"]

    asyncio.run(run())
