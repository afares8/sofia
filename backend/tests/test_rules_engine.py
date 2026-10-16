"""Tests del motor de reglas: la alerta tambien debe crear un issue (para el CLI)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.config import AlertRule, ServiceConfig
from app.services import rules_engine


async def test_rule_alert_also_creates_issue(monkeypatch):
    svc = ServiceConfig(id="mayor", name="Mayor", url="http://x/health", enabled=True)
    rule = AlertRule(id="spike", name="Spike de errores (3x normal)",
                     condition_type="spike", threshold=3.0, window_minutes=60,
                     cooldown_minutes=0)
    monkeypatch.setattr(rules_engine, "load_config", lambda: SimpleNamespace(
        alert_rules=[rule], services=[svc], alerts=object(),
        poll_interval_seconds=30))
    monkeypatch.setattr(rules_engine, "_cooldown", {})
    monkeypatch.setattr(rules_engine.analytics_service, "detect_spike",
                        AsyncMock(return_value=True))
    monkeypatch.setattr(rules_engine.analytics_service, "get_error_rate",
                        AsyncMock(return_value=10))
    monkeypatch.setattr(rules_engine.db_service, "get_recent_error_summary",
                        AsyncMock(return_value=[{"count": 2, "message": "boom"}]))
    upsert = AsyncMock(return_value=(1, True, False))
    monkeypatch.setattr(rules_engine.db_service, "upsert_event", upsert)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(rules_engine.whatsapp_service, "send_alert", send)

    assert await rules_engine.evaluate_rules() == 1
    assert send.await_count == 1
    assert upsert.await_count == 1
    event = upsert.await_args.args[0]
    assert event.service_id == "mayor"
    assert event.level == "WARNING"
    assert event.source == "rules"
    # mensaje estable (sin conteos) para que agrupe en un solo issue
    assert event.message == "[REGLA] Spike de errores (3x normal)"
    assert "Spike de errores" in event.detail


async def test_no_issue_when_rule_does_not_fire(monkeypatch):
    svc = ServiceConfig(id="mayor", name="Mayor", url="http://x/health", enabled=True)
    rule = AlertRule(id="spike", name="Spike", condition_type="spike",
                     threshold=3.0, window_minutes=60, cooldown_minutes=0)
    monkeypatch.setattr(rules_engine, "load_config", lambda: SimpleNamespace(
        alert_rules=[rule], services=[svc], alerts=object(),
        poll_interval_seconds=30))
    monkeypatch.setattr(rules_engine, "_cooldown", {})
    monkeypatch.setattr(rules_engine.analytics_service, "detect_spike",
                        AsyncMock(return_value=False))
    upsert = AsyncMock(return_value=(1, True, False))
    monkeypatch.setattr(rules_engine.db_service, "upsert_event", upsert)
    monkeypatch.setattr(rules_engine.whatsapp_service, "send_alert",
                        AsyncMock(return_value=True))

    assert await rules_engine.evaluate_rules() == 0
    assert upsert.await_count == 0
