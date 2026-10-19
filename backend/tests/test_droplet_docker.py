"""Tests for the droplet docker monitor classification + dedup logic."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services import droplet_docker_service as svc  # noqa: E402


# ── _classify: backend (structlog JSON + raw uvicorn lines) ──────────────────

def test_backend_json_error():
    line = '{"level": "error", "event": "x", "handlers_failed": 0}'
    assert svc._classify("tec-backend-1", line) == "ERROR"


def test_backend_json_critical():
    line = '{"level": "critical", "event": "x"}'
    assert svc._classify("tec-backend-1", line) == "CRITICAL"


def test_backend_json_warning():
    line = '{"level": "warning", "event": "x"}'
    assert svc._classify("tec-backend-1", line) == "WARNING"


def test_backend_json_info_is_not_error():
    line = '{"level": "info", "handlers_failed": 0, "event": "x"}'
    assert svc._classify("tec-backend-1", line) is None


def test_backend_raw_traceback_line():
    line = 'Traceback (most recent call last):'
    assert svc._classify("tec-backend-1", line) == "ERROR"


def test_backend_uvicorn_500_line_is_not_flagged():
    line = 'INFO:     172.19.0.1:56690 - "GET /api/x HTTP/1.1" 200 OK'
    assert svc._classify("tec-backend-1", line) is None


# ── _classify: frontend (nginx) ──────────────────────────────────────────────

def test_nginx_error_asset_404():
    line = ('2026/09/18 16:32:23 [error] 29#29: *3533 open() '
            '"/usr/share/nginx/html/assets/x.js" failed (2: No such file)')
    assert svc._classify("tec-frontend-1", line) == "WARNING"


def test_nginx_asset_probe_is_warning():
    line = ('2026/09/18 16:32:23 [error] 29#29: *3533 open() '
            '"/usr/share/nginx/html/assets/.env" failed (2: No such file or directory)')
    assert svc._classify("tec-frontend-1", line) == "WARNING"


def test_nginx_upstream_error_remains_error():
    line = '2026/09/18 16:32:23 [error] 29#29: connect() failed while connecting to upstream'
    assert svc._classify("tec-frontend-1", line) == "ERROR"


def test_nginx_crit():
    line = '2026/09/18 16:32:23 [crit] 29#29: *3533 something broke'
    assert svc._classify("tec-frontend-1", line) == "CRITICAL"


def test_nginx_warn():
    line = '2026/09/18 16:32:23 [warn] 29#29: something'
    assert svc._classify("tec-frontend-1", line) == "WARNING"


def test_nginx_access_log_not_flagged():
    line = '172.19.0.1 - - [18/Sep/2026:16:32:23 +0000] "GET / HTTP/1.1" 200 5'
    assert svc._classify("tec-frontend-1", line) is None


# ── _classify: postgres ──────────────────────────────────────────────────────

def test_pg_error():
    line = '2026-09-18 17:11:48.005 UTC [1] ERROR:  duplicate key value'
    assert svc._classify("tec-db-1", line) == "ERROR"


def test_pg_fatal():
    line = '2026-09-18 17:11:48.005 UTC [1] FATAL:  connection limit'
    assert svc._classify("tec-db-1", line) == "CRITICAL"


def test_pg_missing_default_role_is_warning():
    line = '2026-09-18 17:11:48 UTC [1] FATAL:  role "postgres" does not exist'
    assert svc._classify("tec-db-1", line) == "WARNING"


def test_pg_lost_client_is_warning():
    line = '2026-09-18 17:11:48 UTC [1] FATAL:  connection to client lost'
    assert svc._classify("tec-db-1", line) == "WARNING"


def test_pg_schema_errors_remain_errors():
    line = '2026-09-18 17:11:48 UTC [1] ERROR:  relation "missing_table" does not exist'
    assert svc._classify("tec-db-1", line) == "ERROR"


def test_pg_log_statement_with_error_columns_is_noise():
    line = ('2026-09-18 17:11:48 UTC [1] LOG:  statement: INSERT INTO t '
            '(last_error, error_code) VALUES')
    assert svc._classify("tec-db-1", line) is None


# ── _classify: generic ───────────────────────────────────────────────────────

def test_generic_error():
    assert svc._classify("some-other", "blah ERROR happened") == "ERROR"


def test_generic_noise_filtered():
    line = '{"handlers_failed": 0, "event": "x"}'
    assert svc._classify("other-container", line) is None


# ── dedup (_is_dup / _mark_seen) ─────────────────────────────────────────────

def test_dedup_same_line_twice():
    svc._last_ts.clear()
    svc._seen_set.clear()
    svc._seen.clear()
    line = "2026/09/18 16:32:23 [error] 29#29: *1 open() x failed"
    assert not svc._is_dup("tec-frontend-1", "2026-09-18T16:32:23Z", line)
    assert svc._is_dup("tec-frontend-1", "2026-09-18T16:32:23Z", line)


def test_dedup_older_timestamp_skipped():
    svc._last_ts.clear()
    svc._last_ts["c1"] = "2026-09-18T17:00:00Z"
    assert svc._is_dup("c1", "2026-09-18T16:00:00Z", "old line")
    assert not svc._is_dup("c1", "2026-09-18T18:00:00Z", "new line")


# ── lifecycle level mapping ─────────────────────────────────────────────────

def test_lifecycle_levels():
    assert svc._LIFECYCLE_LEVELS["oom"] == "CRITICAL"
    assert svc._LIFECYCLE_LEVELS["stop"] == "WARNING"
    assert 0 in svc._LIFECYCLE_CLEAN_EXITS
    assert 137 not in svc._LIFECYCLE_CLEAN_EXITS


def test_ephemeral_container_die_without_exit_code_is_warning(monkeypatch):
    captured = []

    async def store(event):
        captured.append(event)

    monkeypatch.setattr(svc, "_store_and_alert", store)
    asyncio.run(svc._ingest_lifecycle("die", "tec-backend-run-unit-test", None))

    assert captured[0].level == "WARNING"


def test_ephemeral_container_unhealthy_transition_is_warning(monkeypatch):
    captured = []
    name = "tec-backend-run-health-test"
    svc._health.pop(name, None)
    svc._health[name] = "running"

    async def store(event):
        captured.append(event)

    monkeypatch.setattr(svc, "_store_and_alert", store)
    assert asyncio.run(svc._ingest_health_transition(name, "unhealthy"))

    assert captured[0].level == "WARNING"
    svc._health.pop(name, None)


def test_repeated_droplet_issue_does_not_send_another_alert(monkeypatch):
    monkeypatch.setattr(
        svc.db_service, "upsert_event", AsyncMock(return_value=(1, False, False))
    )
    monkeypatch.setattr(svc, "load_config", lambda: SimpleNamespace(alerts=object()))
    send_alert = AsyncMock(return_value=False)
    monkeypatch.setattr(svc.whatsapp_service, "send_alert", send_alert)

    event = svc.ErrorEvent(
        service_id="tec-db",
        service_name="Database",
        level="ERROR",
        message="repeatable SQL failure",
    )
    asyncio.run(svc._store_and_alert(event))

    send_alert.assert_not_awaited()


# ── remote grep covers local classifiers ─────────────────────────────────────

def test_remote_grep_superset_for_known_containers():
    """Every line the local classifier flags must pass the remote grep."""
    import re
    samples = {
        "tec-backend-1": [
            '{"level": "error", "event": "x"}',
            '{"level": "critical", "event": "x"}',
            "Traceback (most recent call last):",
            "2026-09-18 ERROR: something",
        ],
        "tec-frontend-1": [
            "2026/09/18 16:32:23 [error] 29#29: x",
            "2026/09/18 16:32:23 [crit] 29#29: x",
            "2026/09/18 16:32:23 [warn] 29#29: x",
        ],
        "tec-db-1": [
            "2026-09-18 17:11:48 UTC [1] ERROR:  x",
            "2026-09-18 17:11:48 UTC [1] FATAL:  x",
        ],
    }
    for container, lines in samples.items():
        pat = re.compile(svc._remote_grep_pattern(container))
        for line in lines:
            assert pat.search(line), f"remote grep missed: {line}"


# ── fetch de logs por contenedor (cap de lineas + aislamiento) ───────────────

def test_fetch_container_logs_caps_emission_with_tail():
    """`docker logs` debe ir acotado con --tail: sin el cap, un json-log de
    varios GB (tec-db-1 ~2.2 GB) supera el timeout del canal SSH y tumba el
    poll completo."""
    cmds = []

    def fake_exec(client, cmd, timeout=40):
        cmds.append(cmd)
        return "salida"

    logs, failures = svc._fetch_container_logs(
        None, ["tec-db-1"], "240s", exec_fn=fake_exec)
    assert failures == [] and logs["tec-db-1"] == "salida"
    assert f"--tail {svc.LOG_TAIL_CAP}" in cmds[0]
    assert "--since 240s" in cmds[0]
    assert f"tail -{svc.GREP_TAIL_CAP}" in cmds[0]


def test_fetch_container_logs_isolates_slow_container():
    """Un contenedor que revienta (timeout) no debe tumbar el fetch: el resto
    se devuelve igual y la falla queda listada para diagnostico."""
    def fake_exec(client, cmd, timeout=40):
        if "tec-db-1" in cmd:
            raise TimeoutError()
        return "ok"

    logs, failures = svc._fetch_container_logs(
        None, ["tec-db-1", "tec-redis-1"], "240s", exec_fn=fake_exec)
    assert "tec-db-1" not in logs
    assert logs["tec-redis-1"] == "ok"
    assert len(failures) == 1
    assert failures[0][0] == "tec-db-1"
    assert "TimeoutError" in failures[0][1]
