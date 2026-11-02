"""Tests for /api/bridge/escalation (token local + canales de alerta)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.routers import bridge as bridge_router


def _payload(**kw):
    base = dict(group="local", status="uncertain", attempts=3, escalation=1,
                error="ACP error -32011: quota", pending=500, cursor=111157,
                dropped=3, retry_interval_minutes=10)
    base.update(kw)
    return bridge_router.EscalationPayload(**base)


def test_fails_closed_without_token_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SOFIA_BRIDGE_TOKEN_FILE", str(tmp_path / "nope"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bridge_router.bridge_escalation(
            _payload(), x_bridge_token="x"))
    assert exc.value.status_code == 503


def test_rejects_wrong_token(monkeypatch, tmp_path):
    token = tmp_path / "tok"
    token.write_text("s3cret", encoding="utf-8")
    monkeypatch.setenv("SOFIA_BRIDGE_TOKEN_FILE", str(token))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bridge_router.bridge_escalation(
            _payload(), x_bridge_token="otro"))
    assert exc.value.status_code == 403


def test_sends_whatsapp_and_email(monkeypatch, tmp_path):
    token = tmp_path / "tok"
    token.write_text("s3cret", encoding="utf-8")
    monkeypatch.setenv("SOFIA_BRIDGE_TOKEN_FILE", str(token))
    monkeypatch.setattr(bridge_router, "load_config",
                        lambda: SimpleNamespace(alerts=object()))
    wa = AsyncMock(return_value=True)
    monkeypatch.setattr(bridge_router.whatsapp_service, "send_alert", wa)
    mail = lambda subject, body: True
    monkeypatch.setattr(bridge_router.email_alert_service,
                        "send_fallback_email", mail)

    res = asyncio.run(bridge_router.bridge_escalation(
        _payload(), x_bridge_token="s3cret"))

    assert res == {"ok": True, "whatsapp": True, "email": True}
    args = wa.await_args.args
    assert args[1] == "Sofia-Devin (local)"
    assert args[2] == "sofia-devin-local-escalation"
    assert args[3] == "CRITICAL"
    assert "3 intento(s)" in args[4]


def test_email_failure_does_not_fail_the_alert(monkeypatch, tmp_path):
    token = tmp_path / "tok"
    token.write_text("s3cret", encoding="utf-8")
    monkeypatch.setenv("SOFIA_BRIDGE_TOKEN_FILE", str(token))
    monkeypatch.setattr(bridge_router, "load_config",
                        lambda: SimpleNamespace(alerts=object()))
    monkeypatch.setattr(bridge_router.whatsapp_service, "send_alert",
                        AsyncMock(return_value=True))
    monkeypatch.setattr(bridge_router.email_alert_service,
                        "send_fallback_email", lambda s, b: False)

    res = asyncio.run(bridge_router.bridge_escalation(
        _payload(), x_bridge_token="s3cret"))

    assert res == {"ok": True, "whatsapp": True, "email": False}


def test_bridge_can_use_whatsapp_while_global_channel_is_off(monkeypatch,
                                                              tmp_path):
    from app.models.config import AlertConfig

    token = tmp_path / "tok"
    token.write_text("s3cret", encoding="utf-8")
    monkeypatch.setenv("SOFIA_BRIDGE_TOKEN_FILE", str(token))
    alerts = AlertConfig(whatsapp_enabled=False,
                         bridge_escalation_whatsapp=True,
                         whatsapp_number="50700000000")
    monkeypatch.setattr(bridge_router, "load_config",
                        lambda: SimpleNamespace(alerts=alerts))
    wa = AsyncMock(return_value=True)
    monkeypatch.setattr(bridge_router.whatsapp_service, "send_alert", wa)
    monkeypatch.setattr(bridge_router.email_alert_service,
                        "send_fallback_email", lambda s, b: True)

    asyncio.run(bridge_router.bridge_escalation(
        _payload(), x_bridge_token="s3cret"))

    used = wa.await_args.args[0]
    assert used.whatsapp_enabled is True
    assert alerts.whatsapp_enabled is False  # no muta la config global
