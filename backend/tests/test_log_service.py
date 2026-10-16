import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import log_service


@pytest.mark.parametrize(
    ("is_new", "is_regression", "should_alert"),
    [(False, False, False), (True, False, True), (False, True, True)],
)
def test_passive_log_alerts_only_new_issues_or_regressions(
    tmp_path, monkeypatch, is_new, is_regression, should_alert
):
    log_path = tmp_path / "app.log"
    log_path.write_text("ERROR: repeatable failure\n", encoding="utf-8")
    monkeypatch.setitem(log_service._file_positions, str(log_path), 0)
    monkeypatch.setattr(
        log_service.db_service,
        "upsert_event",
        AsyncMock(return_value=(1, is_new, is_regression)),
    )
    monkeypatch.setattr(
        log_service, "load_config", lambda: SimpleNamespace(alerts=object())
    )
    send_alert = AsyncMock(return_value=False)
    monkeypatch.setattr(log_service.whatsapp_service, "send_alert", send_alert)

    asyncio.run(log_service.tail_log("test", "Test", str(log_path)))

    assert send_alert.await_count == int(should_alert)


def test_passive_classification_warns_expected_cancellations():
    assert log_service._classify_level(
        "ERROR: Transition was aborted because of invalid state. Document hidden"
    ) == "WARNING"
    assert log_service._classify_level("WARN: retry scheduled") == "WARNING"
    assert log_service._classify_level(
        "ERROR: [WPPCONNECT] Session Disconnected while sending message"
    ) == "WARNING"
    assert log_service._classify_level("ERROR: invoice update failed") == "ERROR"


def test_ansi_colored_level_is_stripped_and_classified(tmp_path, clean_log_state):
    # WPPConnect escribe \x1b[31merror\x1b[39m: <ts> <msg>; sin strip no se clasifica
    # (el \b de _LEVEL_RE no matchea "merror").
    log = tmp_path / "wpp.out.log"
    log.write_bytes(b"\x1b[31merror\x1b[39m: 2026-10-16T12:37:14.972Z Cannot read properties "
                    b"of undefined (reading 'prepRawMedia')\n")
    events = asyncio.run(log_service.tail_log(
        "wppconnect", "WppConnect", str(log), tail_lines=50))
    assert len(events) == 1
    assert events[0].level == "ERROR"
    assert "\x1b" not in events[0].message
    assert "prepRawMedia" in events[0].message


@pytest.fixture
def clean_log_state(monkeypatch):
    monkeypatch.setattr(log_service, "_file_positions", {})
    monkeypatch.setattr(log_service, "_file_identities", {})
    monkeypatch.setattr(log_service, "_path_locks", {})
    monkeypatch.setattr(log_service, "_resolve_errors", {})
    monkeypatch.setattr(log_service, "_overlong_pending", set())
    monkeypatch.setattr(
        log_service, "_monitor_status", {"last_poll": None, "sources": {}}
    )
    upsert = AsyncMock(return_value=(1, True, False))
    monkeypatch.setattr(log_service.db_service, "upsert_event", upsert)
    monkeypatch.setattr(log_service.db_service, "mark_notified", AsyncMock())
    monkeypatch.setattr(
        log_service, "load_config", lambda: SimpleNamespace(alerts=object())
    )
    send = AsyncMock(return_value=False)
    monkeypatch.setattr(log_service.whatsapp_service, "send_alert", send)
    return SimpleNamespace(upsert=upsert, send=send)


def _status(service_id):
    return log_service.get_monitor_status()["sources"].get(service_id)


def test_missing_custom_path_reports_missing(tmp_path, clean_log_state):
    missing = tmp_path / "nope.log"
    res = asyncio.run(log_service.tail_log("other", "Other", str(missing)))
    assert res == []
    src = _status("other")
    assert src["state"] == "missing"
    assert src["configured_path"] == str(missing)
    assert src["error"]


def test_missing_mayor_custom_path_no_fallback(tmp_path, clean_log_state):
    missing = tmp_path / "custom_missing.log"
    res = asyncio.run(log_service.tail_log("mayor", "Mayor", str(missing)))
    assert res == []
    src = _status("mayor")
    assert src["state"] == "missing"
    assert src["effective_path"] == str(missing)


def test_missing_default_path_uses_nssm_fallback(tmp_path, monkeypatch, clean_log_state):
    default = tmp_path / "missing_dir" / "app.log"
    monkeypatch.setitem(log_service._DEFAULT_LOG_PATHS, "mayor", default)
    fallback = tmp_path / "nssm_stderr.log"
    fallback.write_text("ERROR: via stderr\n", encoding="utf-8")
    monkeypatch.setattr(log_service, "_nssm_stderr_path", lambda sid: fallback)
    events = asyncio.run(log_service.tail_log("mayor", "Mayor", str(default)))
    assert len(events) == 1
    src = _status("mayor")
    assert src["effective_path"] == str(fallback)
    assert src["state"] == "ready"


def test_missing_default_registry_denied_visible(tmp_path, monkeypatch, clean_log_state):
    default = tmp_path / "missing_dir" / "app.log"
    monkeypatch.setitem(log_service._DEFAULT_LOG_PATHS, "packing", default)

    def denied(sid):
        log_service._resolve_errors[sid] = "nssm registry read failed: Access is denied"
        return None

    monkeypatch.setattr(log_service, "_nssm_stderr_path", denied)
    res = asyncio.run(log_service.tail_log("packing", "Packing", str(default)))
    assert res == []
    src = _status("packing")
    assert src["state"] == "missing"
    assert "registry" in src["error"]


def test_info_and_debug_lines_with_error_words_ignored(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_text(
        "INFO: worker finished without ERROR\n"
        "DEBUG: CRITICAL section entered and left fine\n",
        encoding="utf-8",
    )
    events = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert events == []


def test_canonical_messages_match_sdk_fingerprint(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_text(
        "ERROR:app.mod:boom happened\n"
        "2026-10-11 12:00:00 [ERROR] app.mod: second boom\n",
        encoding="utf-8",
    )
    events = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in events] == ["boom happened", "second boom"]


def test_utf8_multibyte_and_deferred_partial_line(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_bytes("ERROR: first 漢字\n".encode("utf-8") + "ERROR: caf".encode("utf-8") + b"\xc3")
    events1 = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert len(events1) == 1
    with open(p, "ab") as f:
        f.write(b"\xa9 done\n")
    events2 = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert len(events2) == 1
    assert events2[0].message.endswith("done")


def test_tail_lines_batches_five_errors_over_three_calls(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_text("".join(f"ERROR: e{i}\n" for i in range(5)), encoding="utf-8")
    e1 = asyncio.run(log_service.tail_log("s", "S", str(p), tail_lines=2))
    e2 = asyncio.run(log_service.tail_log("s", "S", str(p), tail_lines=2))
    e3 = asyncio.run(log_service.tail_log("s", "S", str(p), tail_lines=2))
    messages = [e.message for e in (e1 + e2 + e3)]
    assert messages == ["e0", "e1", "e2", "e3", "e4"]


def test_file_replace_identity_resets_cursor(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_text("ERROR: old\n", encoding="utf-8")
    first = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert len(first) == 1
    replacement = tmp_path / "b.log"
    replacement.write_text("ERROR: fresh\n", encoding="utf-8")
    os.replace(replacement, p)
    second = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in second] == ["fresh"]


def test_file_shrink_resets_cursor(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_text("ERROR: " + "x" * 200 + "\n", encoding="utf-8")
    asyncio.run(log_service.tail_log("s", "S", str(p)))
    p.write_text("ERROR: small\n", encoding="utf-8")
    events = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in events] == ["small"]


def test_db_failure_on_second_record_is_retryable(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_text("ERROR: a\nERROR: b\nERROR: c\n", encoding="utf-8")
    seen = []

    async def upsert(event):
        seen.append(event.message)
        if len(seen) == 2:
            raise RuntimeError("db down")
        return (1, True, False)

    clean_log_state.upsert.side_effect = upsert
    first = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in first] == ["a"]
    second = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in second] == ["b", "c"]
    assert seen == ["a", "b", "b", "c"]


def test_alert_failure_does_not_replay_event(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_text("ERROR: once\n", encoding="utf-8")
    clean_log_state.send.side_effect = RuntimeError("wpp down")
    first = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert len(first) == 1
    second = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert second == []
    assert clean_log_state.upsert.await_count == 1


def test_concurrent_reads_capture_once(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_text("ERROR: shared\n", encoding="utf-8")

    async def run_both():
        return await asyncio.gather(
            log_service.tail_log("s", "S", str(p)),
            log_service.tail_log("s", "S", str(p)),
        )

    e1, e2 = asyncio.run(run_both())
    assert len(e1) + len(e2) == 1
    assert clean_log_state.upsert.await_count == 1


def test_oversized_line_does_not_block_later_error(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_bytes(
        b"ERROR: " + b"x" * (log_service._MAX_LINE_BYTES * 3) + b"\n"
        b"ERROR: real failure\n"
    )
    log_service._file_positions[str(p)] = 0
    events = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert events[-1].message == "real failure"
    src = _status("s")
    assert src["state"] == "error"
    assert "truncated" in src["error"]


def test_state_transition_warns_once(tmp_path, monkeypatch, clean_log_state):
    warn = Mock()
    monkeypatch.setattr(log_service.logger, "warning", warn)
    p = tmp_path / "a.log"
    asyncio.run(log_service.tail_log("s", "S", str(p)))
    p.write_text("ERROR: up\n", encoding="utf-8")
    asyncio.run(log_service.tail_log("s", "S", str(p)))
    asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert warn.call_count == 1


def test_configured_path_stat_error_visible(tmp_path, monkeypatch, clean_log_state):
    from pathlib import Path
    target = tmp_path / "denied.log"
    orig = Path.is_file

    def denied(self):
        if self == target:
            raise PermissionError("denied")
        return orig(self)

    monkeypatch.setattr(Path, "is_file", denied)
    res = asyncio.run(log_service.tail_log("s", "S", str(target)))
    assert res == []
    src = _status("s")
    assert src["state"] in ("missing", "error")
    assert "denied" in src["error"]


def test_fallback_stat_error_visible(tmp_path, monkeypatch, clean_log_state):
    from pathlib import Path
    default = tmp_path / "missing_dir" / "app.log"
    fallback = tmp_path / "denied_stderr.log"
    monkeypatch.setitem(log_service._DEFAULT_LOG_PATHS, "mayor", default)
    monkeypatch.setattr(log_service, "_nssm_stderr_path", lambda sid: fallback)
    orig = Path.is_file

    def denied(self):
        if self == fallback:
            raise PermissionError("denied")
        return orig(self)

    monkeypatch.setattr(Path, "is_file", denied)
    res = asyncio.run(log_service.tail_log("mayor", "Mayor", str(default)))
    assert res == []
    src = _status("mayor")
    assert src["state"] == "missing"
    assert "stat failed" in src["error"]


def test_parser_strips_space_before_logger_prefix():
    reported, message = log_service._split_line("ERROR: app.mod: boom")
    assert reported == "ERROR"
    assert message == "boom"


def test_bare_bracketed_info_ignored():
    assert log_service._classify_level("[INFO] task done without ERROR") is None
    assert log_service._classify_level("[DEBUG] entering CRITICAL path") is None


def test_multiword_prefix_not_stripped_as_logger():
    reported, message = log_service._split_line("ERROR: HTTP 500: upstream failed")
    assert message == "HTTP 500: upstream failed"
    assert log_service._classify_level("ERROR: HTTP 500: upstream failed") == "ERROR"


def test_python_logging_format_uses_reported_level_not_message_words():
    info_line = (
        "2026-10-12 05:28:00,691 - app.pantalla.kpi - INFO - "
        "[WARN] Usando metodo antiguo para 123"
    )
    assert log_service._classify_level(info_line) is None
    warn_line = (
        "2026-10-12 09:48:49,446 - app.security - WARNING - "
        "PROBLEMA DETECTADO: ERROR en cabecera"
    )
    assert log_service._classify_level(warn_line) == "WARNING"
    err_line = "2026-10-12 09:28:13,001 - app.dmc - ERROR - DMCE login rejected"
    assert log_service._classify_level(err_line) == "ERROR"


def test_logger_name_does_not_downgrade_severity():
    level = log_service._classify_level(
        "ERROR: wppconnect: Session Disconnected unexpectedly"
    )
    assert level == "ERROR"


def test_incomplete_final_line_does_not_spin_backlog(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_bytes(b"ERROR: done\nERROR: partial")
    events = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert len(events) == 1
    src = _status("s")
    assert src["state"] == "ready"
    assert src["pending_bytes"] > 0
    assert src["drain_backlog"] is False


def test_full_batch_marks_drain_backlog(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_text("".join(f"ERROR: e{i}\n" for i in range(5)), encoding="utf-8")
    asyncio.run(log_service.tail_log("s", "S", str(p), tail_lines=2))
    assert _status("s")["drain_backlog"] is True
    asyncio.run(log_service.tail_log("s", "S", str(p), tail_lines=2))
    asyncio.run(log_service.tail_log("s", "S", str(p), tail_lines=2))
    assert _status("s")["drain_backlog"] is False


def test_overlong_eof_line_discards_continuation(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_bytes(b"ERROR: " + b"x" * (log_service._MAX_LINE_BYTES * 2))
    log_service._file_positions[str(p)] = 0
    first = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert first == []
    with open(p, "ab") as f:
        f.write(b" tail ERROR\nERROR: next\n")
    second = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in second] == ["next"]


def test_poll_loop_sleeps_interval_without_backlog(monkeypatch, clean_log_state):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise RuntimeError("stop")

    monkeypatch.setattr(log_service.asyncio, "sleep", fake_sleep)
    svc = SimpleNamespace(id="a", name="A", log_path="x", enabled=True)
    monkeypatch.setattr(
        log_service,
        "load_config",
        lambda: SimpleNamespace(
            services=[svc], log_tail_lines=2, poll_interval_seconds=7
        ),
    )
    monkeypatch.setattr(log_service, "tail_log", AsyncMock(return_value=[]))
    with pytest.raises(RuntimeError):
        asyncio.run(log_service.log_poll_loop())
    assert sleeps == [7, 7]


def test_poll_loop_drains_backlog_then_interval(monkeypatch, clean_log_state):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise RuntimeError("stop")

    monkeypatch.setattr(log_service.asyncio, "sleep", fake_sleep)
    svc = SimpleNamespace(id="a", name="A", log_path="x", enabled=True)
    monkeypatch.setattr(
        log_service,
        "load_config",
        lambda: SimpleNamespace(
            services=[svc], log_tail_lines=2, poll_interval_seconds=7
        ),
    )
    calls = []

    async def fake_tail(sid, name, lp, tl):
        calls.append(sid)
        backlog = len(calls) == 1
        log_service._monitor_status["sources"][sid] = {
            "service_id": sid,
            "state": "ready",
            "drain_backlog": backlog,
        }
        return []

    monkeypatch.setattr(log_service, "tail_log", fake_tail)
    with pytest.raises(RuntimeError):
        asyncio.run(log_service.log_poll_loop())
    assert sleeps == [0, 7]


def test_db_failure_before_overlong_replays_all(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_bytes(
        b"ERROR: a\n" + b"x" * (log_service._MAX_LINE_BYTES * 2) + b"\nERROR: b\n"
    )
    log_service._file_positions[str(p)] = 0
    calls = []

    async def upsert(event):
        calls.append(event.message)
        if len(calls) == 1:
            raise RuntimeError("db down")
        return (1, True, False)

    clean_log_state.upsert.side_effect = upsert
    first = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert first == []
    second = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in second] == ["a", "b"]
    assert calls == ["a", "a", "b"]


def test_db_failure_before_overlong_eof_replays(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_bytes(b"ERROR: a\n" + b"x" * (log_service._MAX_LINE_BYTES * 2))
    log_service._file_positions[str(p)] = 0
    calls = []

    async def upsert(event):
        calls.append(event.message)
        if len(calls) == 1:
            raise RuntimeError("db down")
        return (1, True, False)

    clean_log_state.upsert.side_effect = upsert
    assert asyncio.run(log_service.tail_log("s", "S", str(p))) == []
    second = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in second] == ["a"]
    with open(p, "ab") as f:
        f.write(b"\nERROR: b\n")
    third = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in third] == ["b"]


def test_poll_loop_db_failure_uses_normal_interval(
    monkeypatch, tmp_path, clean_log_state
):
    p = tmp_path / "a.log"
    p.write_text("ERROR: a\nERROR: b\nERROR: c\n", encoding="utf-8")
    log_service._file_positions[str(p)] = 0
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise RuntimeError("stop")

    monkeypatch.setattr(log_service.asyncio, "sleep", fake_sleep)
    svc = SimpleNamespace(id="s", name="S", log_path=str(p), enabled=True)
    monkeypatch.setattr(
        log_service,
        "load_config",
        lambda: SimpleNamespace(
            services=[svc], log_tail_lines=1, poll_interval_seconds=7,
            alerts=object(),
        ),
    )
    calls = []

    async def upsert(event):
        calls.append(event.message)
        if len(calls) > 1:
            raise RuntimeError("db down")
        return (1, True, False)

    clean_log_state.upsert.side_effect = upsert
    with pytest.raises(RuntimeError):
        asyncio.run(log_service.log_poll_loop())
    assert sleeps == [0, 7]


def test_replace_clears_overlong_state(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    p.write_bytes(b"ERROR: " + b"x" * (log_service._MAX_LINE_BYTES * 2))
    log_service._file_positions[str(p)] = 0
    asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert str(p) in log_service._overlong_pending
    replacement = tmp_path / "b.log"
    replacement.write_text("ERROR: fresh\n", encoding="utf-8")
    os.replace(replacement, p)
    events = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in events] == ["fresh"]
    assert str(p) not in log_service._overlong_pending


def test_startup_offset_at_line_boundary_not_skipped(tmp_path, clean_log_state):
    p = tmp_path / "a.log"
    prefix = b"X" * 8100 + b"\n"
    rest = b"ERROR: boundary\n" + b"z" * (8192 - 16)
    p.write_bytes(prefix + rest)
    events = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in events] == ["boundary"]


def test_startup_cursor_frozen_across_db_failure_and_growth(
    tmp_path, clean_log_state
):
    p = tmp_path / "a.log"
    p.write_text("ERROR: a\n", encoding="utf-8")
    calls = []

    async def upsert(event):
        calls.append(event.message)
        if len(calls) == 1:
            raise RuntimeError("db down")
        return (1, True, False)

    clean_log_state.upsert.side_effect = upsert
    assert asyncio.run(log_service.tail_log("s", "S", str(p))) == []
    with open(p, "ab") as f:
        f.write(b"ERROR: b\n" + b"z" * 9000)
    second = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in second] == ["a", "b"]


def test_startup_skip_advances_cursor_past_partial_line(
    tmp_path, clean_log_state
):
    p = tmp_path / "a.log"
    p.write_bytes(b"INFO: " + b"x" * 9000 + b" has ERROR text\n")
    first = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert first == []
    with open(p, "ab") as f:
        f.write(b"ERROR: next\n")
    second = asyncio.run(log_service.tail_log("s", "S", str(p)))
    assert [e.message for e in second] == ["next"]


@pytest.mark.parametrize("n", [0, -1])
def test_invalid_tail_lines_exposes_error(tmp_path, clean_log_state, n):
    p = tmp_path / "a.log"
    p.write_text("ERROR: x\n", encoding="utf-8")
    res = asyncio.run(log_service.tail_log("s", "S", str(p), tail_lines=n))
    assert res == []
    src = _status("s")
    assert src["state"] == "error"
    assert "tail_lines" in src["error"]
    assert src["drain_backlog"] is False


def test_poll_loop_removes_disabled_source_status(monkeypatch, clean_log_state):
    log_service._monitor_status["sources"]["ghost"] = {"service_id": "ghost"}
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        raise RuntimeError("stop")

    monkeypatch.setattr(log_service.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        log_service,
        "load_config",
        lambda: SimpleNamespace(services=[], log_tail_lines=2, poll_interval_seconds=7),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(log_service.log_poll_loop())
    assert "ghost" not in log_service.get_monitor_status()["sources"]
