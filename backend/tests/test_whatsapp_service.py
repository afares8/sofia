from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import httpx
import pytest

from app.models.config import AlertConfig
from app.services import db_service, email_alert_service, whatsapp_service as wa


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(wa, "_cooldown", {})
    monkeypatch.setattr(wa, "_hourly_sent", {})
    monkeypatch.setattr(email_alert_service, "send_fallback_email", MagicMock())
    return AlertConfig(whatsapp_enabled=True, whatsapp_number="1234")


async def rows():
    async with aiosqlite.connect(db_service.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return [dict(row) for row in await (await db.execute("SELECT * FROM alert_queue")).fetchall()]


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError])
@pytest.mark.parametrize("alert", [False, True])
async def test_ambiguous_send_is_terminal(initialized_db, cfg, monkeypatch, error, alert):
    failure = wa.DeliveryUncertainError(f"{error.__name__}: lost response")
    failure.__cause__ = error("lost response")
    post = AsyncMock(side_effect=failure)
    monkeypatch.setattr(wa, "_post_message", post)
    if alert:
        assert not await wa.send_alert(cfg, "Test", "test", "ERROR", "message")
        assert not await wa.send_alert(cfg, "Test", "test", "ERROR", "message")
    else:
        assert not await wa.send_message(cfg, "message")
    saved = (await rows())[0]
    assert saved["delivery_uncertain"] == 1
    assert saved["sent_at"] is None
    assert "unknown" in saved["last_error"]
    assert await wa.flush_queue(cfg, max_attempts=100) == 0
    assert post.await_count == 1
    assert email_alert_service.send_fallback_email.called
    assert "desconocida" in email_alert_service.send_fallback_email.call_args.kwargs["body"]


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_pre_send_errors_remain_retryable(initialized_db, cfg, monkeypatch, error):
    post = AsyncMock(side_effect=error("not connected"))
    monkeypatch.setattr(wa, "_post_message", post)
    assert not await wa.send_message(cfg, "message")
    assert len(await db_service.get_pending_alerts()) == 1
    post.side_effect = None
    post.return_value = True
    assert await wa.flush_queue(cfg) == 1
    assert (await rows())[0]["sent_at"] is not None


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError])
async def test_flush_unknown_survives_future_flush_and_purge(initialized_db, cfg, monkeypatch, error):
    await db_service.enqueue_alert("1234@c.us", "message")
    failure = wa.DeliveryUncertainError(f"{error.__name__}: lost response")
    failure.__cause__ = error("lost response")
    post = AsyncMock(side_effect=failure)
    monkeypatch.setattr(wa, "_post_message", post)
    assert await wa.flush_queue(cfg) == 0
    assert await wa.flush_queue(cfg, max_attempts=100) == 0
    assert post.await_count == 1
    assert await db_service.purge_failed_alerts(max_attempts=0) == 0
    saved = (await rows())[0]
    assert saved["delivery_uncertain"] == 1
    assert saved["attempts"] == 1
    assert saved["sent_at"] is None
    assert email_alert_service.send_fallback_email.called


@pytest.mark.parametrize("body", [
    {"status": "Disconnected", "response": None},
    {"status": "Connected", "response": None},
    {"status": "Error", "error": "send failed"},
    {"status": "success", "response": []},
    {},
])
async def test_post_rejects_wpp_failure(cfg, monkeypatch, body):
    monkeypatch.setattr(wa, "get_auth_headers", AsyncMock(return_value={}))
    client = AsyncMock()
    client.post.return_value = httpx.Response(200, json=body, request=httpx.Request("POST", "http://mock/send"))
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = client
    monkeypatch.setattr(wa.httpx, "AsyncClient", factory)
    with pytest.raises(Exception):
        await wa._post_message(cfg, "1234@c.us", "message")
    assert client.post.await_count == 1


async def test_migration_idempotent_preserves_legacy_queue(tmp_sofia_paths):
    async with aiosqlite.connect(db_service.DB_PATH) as db:
        await db.execute("CREATE TABLE alert_queue (id INTEGER PRIMARY KEY, message TEXT, phone TEXT, created_at TEXT, sent_at TEXT, attempts INTEGER DEFAULT 0, last_error TEXT)")
        await db.execute("INSERT INTO alert_queue VALUES (1, 'legacy', '1234', '2099-01-01', NULL, 0, NULL)")
        await db.commit()
    await db_service.init_db()
    await db_service.init_db()
    assert (await rows())[0]["delivery_uncertain"] == 0
    assert len(await db_service.get_pending_alerts()) == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError])
async def test_real_post_path_does_not_retry(initialized_db, cfg, monkeypatch, error):
    monkeypatch.setattr(wa, "get_auth_headers", AsyncMock(return_value={}))
    client = AsyncMock()
    client.post.side_effect = error("response lost")
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = client
    monkeypatch.setattr(wa.httpx, "AsyncClient", factory)
    assert not await wa.send_message(cfg, "message")
    assert await wa.flush_queue(cfg) == 0
    assert client.post.await_count == 1
    assert (await rows())[0]["delivery_uncertain"] == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ReadError, httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
@pytest.mark.parametrize("refresh", [False, True])
async def test_token_failure_is_known_unsent_and_retried(initialized_db, cfg, monkeypatch, error, refresh):
    failure = error("token unavailable")
    auth = AsyncMock(side_effect=[{}, failure] if refresh else failure)
    monkeypatch.setattr(wa, "get_auth_headers", auth)
    client = AsyncMock()
    request = httpx.Request("POST", "http://mock/send")
    client.post.return_value = httpx.Response(401, request=request)
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = client
    monkeypatch.setattr(wa.httpx, "AsyncClient", factory)
    assert not await wa.send_message(cfg, "message")
    assert client.post.await_count == int(refresh)
    saved = (await rows())[0]
    assert saved["delivery_uncertain"] == 0
    assert saved["sent_at"] is None
    assert len(await db_service.get_pending_alerts()) == 1
    assert "desconocida" not in email_alert_service.send_fallback_email.call_args.kwargs["body"]
    auth.side_effect = None
    auth.return_value = {}
    client.post.return_value = httpx.Response(201, json={"status": "success", "response": [{"id": "mock-id"}]}, request=request)
    assert await wa.flush_queue(cfg) == 1
    assert client.post.await_count == int(refresh) + 1
    assert (await rows())[0]["sent_at"] is not None


async def test_message_retry_after_401_timeout_is_unknown(initialized_db, cfg, monkeypatch):
    auth = AsyncMock(return_value={})
    monkeypatch.setattr(wa, "get_auth_headers", auth)
    client = AsyncMock()
    client.post.side_effect = [httpx.Response(401, request=httpx.Request("POST", "http://mock/send")), httpx.ReadTimeout("response lost")]
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = client
    monkeypatch.setattr(wa.httpx, "AsyncClient", factory)
    assert not await wa.send_message(cfg, "message")
    assert auth.await_count == 2
    assert auth.call_args.kwargs["force_refresh"] is True
    assert client.post.await_count == 2
    assert (await rows())[0]["delivery_uncertain"] == 1
    assert await wa.flush_queue(cfg) == 0
    assert client.post.await_count == 2


@pytest.mark.parametrize("status,body,uncertain", [
    (201, {"status": "success", "response": [{"id": "mock-id"}]}, None),
    (500, {"status": "Error", "error": "send failed"}, 1),
    (200, {"status": "Error", "error": "send failed"}, 1),
    (404, {"status": "Disconnected", "response": None}, 0),
    (400, {"status": "Connected", "response": None}, 0),
])
async def test_server_response_delivery_state(initialized_db, cfg, monkeypatch, status, body, uncertain):
    monkeypatch.setattr(wa, "get_auth_headers", AsyncMock(return_value={}))
    client = AsyncMock()
    client.post.return_value = httpx.Response(status, json=body, request=httpx.Request("POST", "http://mock/send"))
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = client
    monkeypatch.setattr(wa.httpx, "AsyncClient", factory)
    assert await wa.send_message(cfg, "message") is (uncertain is None)
    saved = await rows()
    if uncertain is None:
        assert not saved
    else:
        assert saved[0]["delivery_uncertain"] == uncertain
        assert saved[0]["sent_at"] is None
    assert client.post.await_count == 1
