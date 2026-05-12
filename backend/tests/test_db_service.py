"""DB-layer tests: init, upsert, regression detection, metrics, restores."""
import pytest

from app.models.event import ErrorEvent


pytestmark = pytest.mark.asyncio


async def test_init_and_basic_upsert(initialized_db):
    db = initialized_db
    e = ErrorEvent(service_id="svc", service_name="Service", level="ERROR", message="boom")
    issue_id, is_new, is_regression = await db.upsert_event(e)
    assert isinstance(issue_id, int)
    assert is_new is True
    assert is_regression is False


async def test_duplicate_event_increments_count(initialized_db):
    db = initialized_db
    e = ErrorEvent(service_id="svc", service_name="Service", level="ERROR", message="boom")
    iid1, new1, _ = await db.upsert_event(e)
    iid2, new2, _ = await db.upsert_event(e)
    assert iid1 == iid2
    assert new1 is True
    assert new2 is False


async def test_regression_detection(initialized_db):
    db = initialized_db
    e = ErrorEvent(service_id="svc", service_name="Service", level="ERROR", message="boom")
    iid, _, _ = await db.upsert_event(e)
    await db.resolve_issue(iid)
    iid2, is_new, is_reg = await db.upsert_event(e)
    assert iid == iid2
    assert is_new is False
    assert is_reg is True


async def test_record_and_query_metrics(initialized_db):
    db = initialized_db
    await db.record_metric("svc", 120.5, 200, True)
    await db.record_metric("svc", 250.0, 200, True)
    await db.record_metric("svc", 0,    500, False)
    metrics = await db.get_metrics("svc", since_hours=24)
    assert len(metrics) == 3
    stats = await db.get_response_stats("svc", since_hours=24)
    assert stats["total_checks"] == 3
    uptime = await db.get_uptime_percent("svc", since_hours=24)
    assert 50 < uptime < 80  # 2 ups out of 3 = ~66.7%


async def test_save_and_get_restore_history(initialized_db):
    db = initialized_db
    rid = await db.save_restore({
        "service_id": "svc",
        "service_name": "Service",
        "status": "success",
        "trigger_mode": "auto",
        "requested_at": "2024-01-01T00:00:00",
        "finished_at":  "2024-01-01T00:01:00",
        "result_message": "ok",
    })
    assert rid > 0
    hist = await db.get_restore_history(limit=10)
    assert len(hist) == 1
    assert hist[0]["service_id"] == "svc"
    assert hist[0]["trigger_mode"] == "auto"


async def test_alert_queue_lifecycle(initialized_db):
    db = initialized_db
    qid = await db.enqueue_alert("123@c.us", "hola")
    assert qid > 0
    pending = await db.get_pending_alerts()
    assert len(pending) == 1
    assert pending[0]["message"] == "hola"
    await db.mark_alert_sent(qid)
    assert await db.get_pending_alerts() == []
