"""Tests for the local host monitor classification logic."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services import local_host_service as svc  # noqa: E402


def test_critical_event_maps_to_critical():
    ev = {"ProviderName": "Microsoft-Windows-Kernel-Power", "Id": 41,
          "LevelDisplayName": "Critical"}
    assert svc._classify_event(ev) == "CRITICAL"


def test_scm_unexpected_termination_is_error():
    ev = {"ProviderName": "Service Control Manager", "Id": 7031,
          "LevelDisplayName": "Error"}
    assert svc._classify_event(ev) == "ERROR"


def test_scm_generic_error_is_warning():
    ev = {"ProviderName": "Service Control Manager", "Id": 7000,
          "LevelDisplayName": "Error"}
    assert svc._classify_event(ev) == "WARNING"


def test_distributedcom_noise_ignored():
    ev = {"ProviderName": "Microsoft-Windows-DistributedCOM", "Id": 10016,
          "LevelDisplayName": "Error"}
    assert svc._classify_event(ev) is None


def test_app_crash_is_error():
    ev = {"ProviderName": "Application Error", "Id": 1000,
          "LevelDisplayName": "Error"}
    assert svc._classify_event(ev) == "ERROR"


def test_disk_error_source_is_error():
    ev = {"ProviderName": "Ntfs", "Id": 55, "LevelDisplayName": "Error"}
    assert svc._classify_event(ev) == "ERROR"


def test_unknown_error_source_is_warning():
    ev = {"ProviderName": "SomeRandomProvider", "Id": 500,
          "LevelDisplayName": "Error"}
    assert svc._classify_event(ev) == "WARNING"


def test_dotnet_profiler_attach_event_is_warning():
    ev = {"ProviderName": ".NET Runtime", "Id": 1022,
          "LevelDisplayName": "Error"}
    assert svc._classify_event(ev) == "WARNING"


def test_watched_services_present():
    for name in ("MayorBackend", "PackingBackend", "WPPConnect",
                 "SofiaTunnel", "NgrokTunnel", "RustDesk"):
        assert name in svc.WATCHED_SERVICES


def test_new_watched_services_present():
    for name in ("VisionBackend", "VisionMedia", "vms-api"):
        assert name in svc.WATCHED_SERVICES


def test_collect_service_states_queries_new_names(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[2])
        return SimpleNamespace(
            stdout="        STATE              : 4  RUNNING\n", returncode=0
        )

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    states = svc._collect_service_states()
    for name in ("VisionBackend", "VisionMedia", "vms-api"):
        assert name in calls
        assert states[name] == "RUNNING"


def test_stopped_watched_service_triggers_error(monkeypatch):
    monkeypatch.setitem(svc._svc_state, "vms-api", "RUNNING")
    host_event = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "_host_event", host_event)

    count = asyncio.run(svc._process_services({"vms-api": "STOPPED"}))

    assert count == 1
    host_event.assert_awaited_once_with(
        "ERROR", "svc_vms-api", "Windows service vms-api is STOPPED"
    )


def test_paused_service_transition_is_warning(monkeypatch):
    service_name = "test-paused-service"
    monkeypatch.setitem(svc._svc_state, service_name, "RUNNING")
    host_event = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "_host_event", host_event)

    count = asyncio.run(svc._process_services({service_name: "PAUSED"}))

    assert count == 1
    host_event.assert_awaited_once_with(
        "WARNING", f"svc_{service_name}",
        f"Windows service {service_name} is PAUSED",
    )


def test_repeated_local_issue_does_not_send_another_alert(monkeypatch):
    monkeypatch.setattr(
        svc.db_service, "upsert_event", AsyncMock(return_value=(1, False, False))
    )
    monkeypatch.setattr(svc, "load_config", lambda: SimpleNamespace(alerts=object()))
    send_alert = AsyncMock(return_value=False)
    monkeypatch.setattr(svc.whatsapp_service, "send_alert", send_alert)

    event = svc.ErrorEvent(
        service_id="server-sofia",
        service_name="Local host",
        level="ERROR",
        message="repeatable host event",
    )
    asyncio.run(svc._store_and_alert(event))

    send_alert.assert_not_awaited()



def test_collect_events_groups_both_queries(monkeypatch):
    monkeypatch.setattr(svc, "_last_record_id", {})
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(stdout='[{"RecordId": 1}]', returncode=0)

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    svc._collect_events()

    cmd = captured["argv"][-1]
    assert cmd.startswith("[Console]::OutputEncoding=")
    assert "@(" in cmd
    assert "LogName='System'" in cmd.split("@(")[1].split(") |")[0]
    assert "LogName='Application'" in cmd.split("@(")[1].split(") |")[0]
    assert "ConvertTo-Json" in cmd
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"
    assert captured["kwargs"]["timeout"] == 60


def test_collect_events_parses_list_singleton_empty(monkeypatch):
    def fake(stdout):
        return lambda argv, **k: SimpleNamespace(stdout=stdout, returncode=0)

    monkeypatch.setattr(svc.subprocess, "run", fake(
        '[{"RecordId": 1, "LogName": "System"},'
        ' {"RecordId": 2, "LogName": "Application"}]'))
    assert [e["RecordId"] for e in svc._collect_events()] == [1, 2]

    monkeypatch.setattr(svc.subprocess, "run",
                        fake('{"RecordId": 9, "LogName": "System"}'))
    assert [e["RecordId"] for e in svc._collect_events()] == [9]

    monkeypatch.setattr(svc.subprocess, "run", fake(""))
    assert svc._collect_events() == []

    monkeypatch.setattr(svc.subprocess, "run", fake("garbage text"))
    assert svc._collect_events() == []


def test_collect_events_uses_cursor_xpath_for_known_log(monkeypatch):
    monkeypatch.setattr(svc, "_last_record_id",
                        {"System": 37, "Application": 0})
    captured = {}

    def fake_run(argv, **kwargs):
        captured["cmd"] = argv[-1]
        return SimpleNamespace(stdout="[]", returncode=0)

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    svc._collect_events()
    cmd = captured["cmd"]
    assert "EventRecordID > 37" in cmd and "-Oldest" in cmd
    assert "-LogName 'System' -FilterXPath" in cmd
    assert "LogName='Application';Level=1,2;StartTime=" in cmd
    assert cmd.count("-Oldest") == 1


def test_collect_events_mixed_cursor_and_initial_window(monkeypatch):
    monkeypatch.setattr(svc, "_last_record_id", {"Application": 90})
    captured = {}

    def fake_run(argv, **kwargs):
        captured["cmd"] = argv[-1]
        return SimpleNamespace(stdout="[]", returncode=0)

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    svc._collect_events()
    cmd = captured["cmd"]
    assert "EventRecordID > 90" in cmd
    assert "LogName='System';Level=1,2;StartTime=" in cmd


def test_aged_event_survives_outage_via_cursor(monkeypatch):
    _reset_host_state(monkeypatch)
    monkeypatch.setitem(svc._last_record_id, "Application", 5)
    old = _ev(10, "crash before outage\nException code: 0xc0000005")
    persisted = []
    attempts = {"n": 0}

    async def upsert(event):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("db gone")
        persisted.append(event)
        return (1, True, False)

    monkeypatch.setattr(svc.db_service, "upsert_event", upsert)
    monkeypatch.setattr(svc, "load_config", lambda: SimpleNamespace(alerts=object()))
    monkeypatch.setattr(svc.whatsapp_service, "send_alert", AsyncMock(return_value=False))

    import pytest
    with pytest.raises(RuntimeError):
        asyncio.run(svc._process_events([old]))
    captured = {}

    def fake_run(argv, **kwargs):
        captured["cmd"] = argv[-1]
        return SimpleNamespace(stdout="[]", returncode=0)

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    svc._collect_events()
    assert "EventRecordID > 5" in captured["cmd"]
    count = asyncio.run(svc._process_events([old]))
    assert count == 1
    assert len(persisted) == 1
    assert svc._last_record_id["Application"] == 10



def _reset_host_state(monkeypatch):
    monkeypatch.setattr(svc, "_seen_ids", set())
    monkeypatch.setattr(svc, "_last_record_id", {})


def _stub_store(monkeypatch):
    calls = []

    async def fake_store(event):
        calls.append(event)

    monkeypatch.setattr(svc, "_store_and_alert", fake_store)
    return calls


def test_dotnet_multiline_detail_and_causal_summary(monkeypatch):
    _reset_host_state(monkeypatch)
    calls = _stub_store(monkeypatch)
    message = (
        "Application: MyApp.exe\n"
        "Framework Version: v4.0.30319\n"
        "Exception Info: System.NullReferenceException\n"
        "   at MyApp.Program.Main() in C:\\src\\Program.cs:line 42\n"
    )
    ev = {"RecordId": 10, "LogName": "Application", "Id": 1026,
          "ProviderName": ".NET Runtime", "LevelDisplayName": "Error",
          "Message": message, "TimeCreated": "2026-10-11T10:00:00Z"}
    count = asyncio.run(svc._process_events([ev]))
    assert count == 1
    event = calls[0]
    assert "Exception Info: System.NullReferenceException" in event.message
    detail = json.loads(event.detail)
    assert detail["record_id"] == 10
    assert detail["log_name"] == "Application"
    assert detail["event_id"] == 1026
    assert detail["provider"] == ".NET Runtime"
    assert detail["source_time"] == "2026-10-11T10:00:00Z"
    assert "Program.cs:line 42" in detail["message"]
    assert detail["message_truncated"] is False


def test_native_crash_code_in_summary(monkeypatch):
    _reset_host_state(monkeypatch)
    calls = _stub_store(monkeypatch)
    message = (
        "Faulting application name: VisionMedia.exe, version: 1.2.3\n"
        "Faulting module name: codec.dll\n"
        "Exception code: 0xc0000005\n"
        "Faulting module name: codec.dll\n"
    )
    ev = {"RecordId": 11, "LogName": "Application", "Id": 1000,
          "ProviderName": "Application Error", "LevelDisplayName": "Error",
          "Message": message, "TimeCreated": "t"}
    asyncio.run(svc._process_events([ev]))
    msg = calls[0].message
    assert "Faulting module name: codec.dll" in msg
    assert "Exception code: 0xc0000005" in msg
    assert msg.index("Faulting module name:") < msg.index("Exception code:")
    assert msg.count("codec.dll") == 1


def test_redaction_in_message_and_detail(monkeypatch):
    _reset_host_state(monkeypatch)
    calls = _stub_store(monkeypatch)
    message = (
        'snapshot failed rtsp://operator:s3cret@192.168.0.10:554/cam '
        'https://cam.local/api?token=abc123 user@corp.com '
        '"phone": "+521234567890" '
        'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl'
    )
    ev = {"RecordId": 12, "LogName": "Application", "Id": 1000,
          "ProviderName": "Application Error", "LevelDisplayName": "Error",
          "Message": message, "TimeCreated": "t"}
    asyncio.run(svc._process_events([ev]))
    event = calls[0]
    combined = event.message + event.detail
    for secret in ("s3cret", "abc123", "user@corp.com", "521234567890", "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in combined
    assert "[REDACTADO]" in combined or "REDACTADO" in combined


def test_huge_message_truncated_valid_json(monkeypatch):
    _reset_host_state(monkeypatch)
    calls = _stub_store(monkeypatch)
    message = "boom\n" + ("x" * 15000)
    ev = {"RecordId": 13, "LogName": "System", "Id": 55,
          "ProviderName": "Ntfs", "LevelDisplayName": "Error",
          "Message": message, "TimeCreated": "t"}
    asyncio.run(svc._process_events([ev]))
    detail = json.loads(calls[0].detail)
    assert detail["message_truncated"] is True
    assert len(detail["message"]) <= 12000



def _ev(rid, msg=None):
    msg = msg or f"failure signature {rid}"
    return {"RecordId": rid, "LogName": "Application", "Id": 1000,
            "ProviderName": "Application Error", "LevelDisplayName": "Error",
            "Message": msg, "TimeCreated": "t"}


def test_db_failure_then_retry_persists_both_once(monkeypatch):
    _reset_host_state(monkeypatch)
    persisted = []
    attempts = {"n": 0}

    async def upsert(event):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("db gone")
        persisted.append(event)
        return (1, True, False)

    monkeypatch.setattr(svc.db_service, "upsert_event", upsert)
    monkeypatch.setattr(svc, "load_config", lambda: SimpleNamespace(alerts=object()))
    monkeypatch.setattr(svc.whatsapp_service, "send_alert", AsyncMock(return_value=False))

    import pytest
    with pytest.raises(RuntimeError):
        asyncio.run(svc._process_events([_ev(20), _ev(21)]))
    assert svc._last_record_id.get("Application", 0) == 0

    count = asyncio.run(svc._process_events([_ev(20), _ev(21)]))
    assert count == 2
    assert len(persisted) == 2
    assert svc._last_record_id["Application"] == 21


def test_failure_on_one_record_does_not_skip_later(monkeypatch):
    _reset_host_state(monkeypatch)
    persisted = []
    calls = {"n": 0}

    async def upsert(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db gone")
        persisted.append(event)
        return (1, True, False)

    monkeypatch.setattr(svc.db_service, "upsert_event", upsert)
    monkeypatch.setattr(svc, "load_config", lambda: SimpleNamespace(alerts=object()))
    monkeypatch.setattr(svc.whatsapp_service, "send_alert", AsyncMock(return_value=False))

    import pytest
    with pytest.raises(RuntimeError):
        asyncio.run(svc._process_events([_ev(30)]))
    count = asyncio.run(svc._process_events([_ev(30), _ev(31), _ev(32)]))
    assert count == 3
    assert len(persisted) == 3
    assert svc._last_record_id["Application"] == 32


def test_alert_failure_after_persist_no_replay(monkeypatch):
    _reset_host_state(monkeypatch)
    persisted = []

    async def upsert(event):
        persisted.append(event)
        return (5, True, False)

    monkeypatch.setattr(svc.db_service, "upsert_event", upsert)
    monkeypatch.setattr(svc, "load_config", lambda: SimpleNamespace(alerts=object()))
    send_alert = AsyncMock(side_effect=RuntimeError("wpp down"))
    monkeypatch.setattr(svc.whatsapp_service, "send_alert", send_alert)
    mark_notified = AsyncMock()
    monkeypatch.setattr(svc.db_service, "mark_notified", mark_notified)

    count = asyncio.run(svc._process_events([_ev(40)]))
    assert count == 1
    assert len(persisted) == 1
    assert mark_notified.await_count == 0

    count2 = asyncio.run(svc._process_events([_ev(40)]))
    assert count2 == 0
    assert len(persisted) == 1


def test_noise_advances_cursor_without_alert(monkeypatch):
    _reset_host_state(monkeypatch)
    calls = _stub_store(monkeypatch)
    noise = {"RecordId": 50, "LogName": "System", "Id": 10016,
             "ProviderName": "Microsoft-Windows-DistributedCOM",
             "LevelDisplayName": "Error", "Message": "dcom noise",
             "TimeCreated": "t"}
    count = asyncio.run(svc._process_events([noise]))
    assert count == 0
    assert svc._last_record_id["System"] == 50
    assert calls == []
