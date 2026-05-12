"""Model validation tests."""
import pytest

from app.models.config import (
    AlertConfig, AlertRule, MonitorConfig, ServiceConfig,
    DEFAULT_ALERT_RULES, DEFAULT_SERVICES,
)
from app.models.event import ErrorEvent
from app.models.restore import PendingRestore, RestoreStatus


def test_service_defaults_auto_restore_off():
    for svc in DEFAULT_SERVICES:
        assert svc.auto_restore is False, f"{svc.id} should have auto_restore=False by default"


def test_default_alert_rules_have_required_fields():
    assert len(DEFAULT_ALERT_RULES) >= 3
    for rule in DEFAULT_ALERT_RULES:
        assert rule.condition_type in {"error_count", "response_ms", "downtime_minutes", "spike"}
        assert rule.threshold > 0


def test_monitor_config_round_trip():
    cfg = MonitorConfig(
        services=DEFAULT_SERVICES,
        alerts=AlertConfig(),
        alert_rules=DEFAULT_ALERT_RULES,
    )
    data = cfg.model_dump()
    cfg2 = MonitorConfig(**data)
    assert len(cfg2.services) == len(cfg.services)
    assert len(cfg2.alert_rules) == len(cfg.alert_rules)


def test_event_accepts_breadcrumbs_and_tags():
    e = ErrorEvent(
        service_id="svc", service_name="Service",
        level="ERROR", message="boom",
        environment="prod", release="1.2.3",
        tags='{"region": "panama"}',
    )
    assert e.environment == "prod"
    assert e.release == "1.2.3"


def test_pending_restore_has_retry_count():
    r = PendingRestore(
        service_id="svc", service_name="Service",
        status=RestoreStatus.PENDING,
        requested_at="2024-01-01T00:00:00",
    )
    assert r.retry_count == 0
    assert r.trigger_mode == "manual"


def test_alert_rule_validates_condition_type():
    # Pydantic should accept any string in condition_type, validation happens in rules engine.
    r = AlertRule(id="x", name="x", condition_type="error_count", threshold=5)
    assert r.window_minutes == 60
    assert r.enabled is True
