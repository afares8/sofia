"""Analytics: error rate windows and spike detection."""
import pytest


pytestmark = pytest.mark.asyncio


async def test_error_rate_in_window(initialized_db):
    from app.services import analytics_service
    from app.models.event import ErrorEvent
    db = initialized_db
    for _ in range(3):
        await db.upsert_event(ErrorEvent(
            service_id="svc", service_name="Service",
            level="ERROR", message="boom",
        ))
    count = await analytics_service.get_error_rate("svc", window_minutes=60)
    assert count >= 1  # at least one issue with multiple occurrences in the window


async def test_get_error_rates_by_service(initialized_db):
    from app.services import analytics_service
    from app.services.config_service import load_config
    from app.models.event import ErrorEvent
    db = initialized_db
    cfg = load_config()
    assert len(cfg.services) >= 1
    first = cfg.services[0]
    await db.upsert_event(ErrorEvent(
        service_id=first.id, service_name=first.name,
        level="ERROR", message="m",
    ))
    rates = await analytics_service.get_error_rates_by_service(window_minutes=60)
    assert isinstance(rates, dict)
    # The configured service should appear in the breakdown.
    assert first.id in rates


async def test_detect_spike_no_history_is_false(initialized_db):
    from app.services import analytics_service
    is_spike = await analytics_service.detect_spike("svc")
    assert is_spike is False
