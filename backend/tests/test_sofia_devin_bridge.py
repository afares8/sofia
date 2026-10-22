"""Tests aislados del puente Sofia-Devin de escritorio."""
import io
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.scripts import sofia_devin_bridge as bridge

REPO_ROOT = Path(bridge.__file__).resolve().parents[3]
AUTH_POLICY_DIR = REPO_ROOT / ".devin" / "sofia-cli"

ISSUES_DDL = """
CREATE TABLE issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id TEXT NOT NULL, service_name TEXT NOT NULL, level TEXT NOT NULL,
    message TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'active',
    environment TEXT, release TEXT
)
"""
OCC_DDL = """
CREATE TABLE occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL, timestamp TEXT NOT NULL,
    detail TEXT, traceback TEXT, breadcrumbs TEXT
)
"""


def make_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(ISSUES_DDL)
    conn.execute(OCC_DDL)
    conn.commit()
    conn.close()


def add_occurrence(db, service_id="sofia_frontend", service_name="Sofia Frontend",
                   level="ERROR", message="boom", source="frontend",
                   environment="prod", release="1.0", detail="detalle",
                   timestamp="2026-01-01T10:00:00"):
    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        "INSERT INTO issues (service_id, service_name, level, message, source, environment, release)"
        " VALUES (?,?,?,?,?,?,?)",
        (service_id, service_name, level, message, source, environment, release))
    issue_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO occurrences (issue_id, timestamp, detail) VALUES (?,?,?)",
        (issue_id, timestamp, detail))
    occ_id = cur.lastrowid
    conn.commit()
    conn.close()
    return issue_id, occ_id


def add_occurrence_for_issue(db, issue_id, detail="detalle",
                             timestamp="2026-01-01T10:05:00"):
    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        "INSERT INTO occurrences (issue_id, timestamp, detail) VALUES (?,?,?)",
        (issue_id, timestamp, detail))
    occ_id = cur.lastrowid
    conn.commit()
    conn.close()
    return occ_id


class Simple:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def env(tmp_path):
    policy_dir = tmp_path / "pol"
    policy_dir.mkdir()
    for name in ("occurrences.sql", "highwater.sql", "local-initial.txt",
                 "elenatec-initial.txt"):
        (policy_dir / name).write_bytes((AUTH_POLICY_DIR / name).read_bytes())
    db = tmp_path / "sofia.db"
    make_db(db)
    devin = tmp_path / "devin.exe"
    devin.write_bytes(b"fake")
    policy = json.loads((AUTH_POLICY_DIR / "policy.json").read_text(encoding="utf-8"))
    policy["source_db"] = str(db)
    policy["state_root"] = str(tmp_path / "state")
    policy["devin_executable"] = str(devin)
    policy["python_executable"] = str(tmp_path / "python.exe")
    policy["poll_seconds"] = 0.01
    # La ventana de acumulacion (espera de 1 min) se prueba en sus propios
    # tests con reloj controlado; el resto corre sin espera.
    policy["batch_hold_seconds"] = 0
    policy.pop("health_ports", None)  # no abrir puertos reales en tests
    (policy_dir / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    loaded = bridge.load_policy(policy_dir)
    return Simple(tmp=tmp_path, db=db, policy_dir=policy_dir, policy=loaded,
                  state_root=tmp_path / "state", devin=devin)


def ev(occ_id, day="2026-01-01", message="m", level="ERROR"):
    return bridge.event_from_row({
        "occurrence_id": occ_id, "issue_id": occ_id,
        "occurrence_timestamp": "2026-01-01T10:00:00",
        "service_id": "s", "service_name": "s", "level": level,
        "message": message, "source": "x", "issue_environment": "",
        "issue_release": "", "detail": "", "traceback": "", "breadcrumbs": ""},
        received_day=day)


def make_watcher(env, group="local", **kw):
    source = bridge.OccurrenceSource(
        env.db, env.policy["_occurrences_sql"], env.policy["_highwater_sql"])
    store = bridge.StateStore(env.state_root / group)
    spawns = []
    kw.setdefault("spawn_worker", lambda p, g, d: spawns.append((g, d)) or FakeChild())
    kw.setdefault("is_worker_alive", lambda g: False)
    kw.setdefault("now", lambda: 1000.0)
    kw.setdefault("today", lambda: "2026-01-01")
    w = bridge.Watcher(env.policy, group, source, store, **kw)
    return w, store, spawns


class FakeChild:
    def __init__(self, rc=None):
        self._rc = rc

    def poll(self):
        return self._rc


class FakeAcpProc:
    def __init__(self, load_session=True, hold_prompt=None, prompt_hook=None,
                 die_on_prompt=False, load_error=None, init_error=None,
                 stop_reason="end_turn", init_protocol=1,
                 mode_value="accept-edits", mode_reject=False,
                 mode_ack_mismatch=False, mode_option_missing=False):
        self.load_session_supported = load_session
        self.hold_prompt = hold_prompt
        self.prompt_hook = prompt_hook
        self.die_on_prompt = die_on_prompt
        self.load_error = load_error
        self.init_error = init_error
        self.stop_reason = stop_reason
        self.init_protocol = init_protocol
        self.mode_value = mode_value
        self.mode_reject = mode_reject
        self.mode_ack_mismatch = mode_ack_mismatch
        self.mode_option_missing = mode_option_missing
        self._in_r, self._in_w = os.pipe()
        self._out_r, self._out_w = os.pipe()
        self._err_r, self._err_w = os.pipe()
        self.stdin = os.fdopen(self._in_w, "wb")
        self.stdout = os.fdopen(self._out_r, "rb")
        self.stderr = os.fdopen(self._err_r, "rb")
        self._out_file = os.fdopen(self._out_w, "wb")
        self.received = []
        self.cancelled = threading.Event()
        self.rpc_responses = {}
        self._rpc_events = {}
        self._next_srv_id = 900
        self._session_counter = 0
        self.terminated = False
        self.spawn_env = None
        self.spawn_cwd = None
        self._dead = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _write(self, msg):
        try:
            self._out_file.write((json.dumps(msg) + "\n").encode("utf-8"))
            self._out_file.flush()
        except (OSError, ValueError):
            pass

    def _wait_response(self, req_id, timeout=15):
        ev_ = threading.Event()
        self._rpc_events[req_id] = ev_
        ev_.wait(timeout)
        return self.rpc_responses.pop(req_id, None)

    def _serve(self):
        f = os.fdopen(self._in_r, "rb")
        try:
            for raw in iter(f.readline, b""):
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if "method" not in msg:
                    self.rpc_responses[msg.get("id")] = msg
                    ev_ = self._rpc_events.get(msg.get("id"))
                    if ev_:
                        ev_.set()
                    continue
                self.received.append(msg)
                self._handle(msg)
        except (OSError, ValueError):
            pass
        finally:
            self._dead.set()
            try:
                self._out_file.close()
            except OSError:
                pass

    def _handle(self, msg):
        method = msg["method"]
        rid = msg.get("id")
        if method == "initialize":
            if self.init_error:
                self._write({"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32000, "message": self.init_error}})
                return
            self._write({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": self.init_protocol,
                "agentCapabilities": {"loadSession": self.load_session_supported},
                "agentInfo": {"name": "fake-devin", "version": "0"}}})
        elif method == "session/new":
            self._session_counter += 1
            self._write({"jsonrpc": "2.0", "id": rid,
                         "result": self._session_result(
                             f"sess-{self._session_counter}")})
        elif method == "session/load":
            if self.load_error:
                self._write({"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32000, "message": self.load_error}})
            else:
                self._write({"jsonrpc": "2.0", "id": rid,
                             "result": self._session_result(
                                 msg["params"]["sessionId"])})
        elif method == "session/set_config_option":
            params = msg["params"]
            if self.mode_reject:
                self._write({"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32000,
                                       "message": "config rechazada"}})
                return
            if params.get("configId") == "mode" \
                    and params.get("value") in (
                            "accept-edits", "smart", "bypass"):
                self.mode_value = params["value"] \
                    if not self.mode_ack_mismatch else "accept-edits"
            self._write({"jsonrpc": "2.0", "id": rid,
                         "result": {
                             "configOptions": self._config_options()}})
        elif method == "session/prompt":
            threading.Thread(target=self._serve_prompt, args=(msg,), daemon=True).start()
        elif method == "session/cancel":
            self.cancelled.set()
            if rid is not None:
                self._write({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            if rid is not None:
                self._write({"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32601, "message": "desconocido"}})

    def _config_options(self):
        opts = []
        if not self.mode_option_missing:
            opts.append({
                "id": "mode", "name": "Session Mode", "category": "mode",
                "type": "select", "currentValue": self.mode_value,
                "options": [
                    {"value": "accept-edits", "name": "Code"},
                    {"value": "smart", "name": "Smart"},
                    {"value": "ask", "name": "Ask"},
                    {"value": "plan", "name": "Plan"},
                    {"value": "bypass", "name": "Bypass Permissions"}]})
        return opts

    def _session_result(self, sid):
        return {"sessionId": sid,
                "modes": {"currentModeId": self.mode_value,
                          "availableModes": [
                              {"id": "accept-edits", "name": "Code"},
                              {"id": "smart", "name": "Smart"},
                              {"id": "ask", "name": "Ask"},
                              {"id": "plan", "name": "Plan"},
                              {"id": "bypass", "name": "Bypass Permissions"}]},
                "configOptions": self._config_options()}

    def _serve_prompt(self, msg):
        rid = msg["id"]
        if self.die_on_prompt:
            try:
                self.stdin.close()
            except OSError:
                pass
            return
        if self.prompt_hook:
            self.prompt_hook(msg, self)
        if self.hold_prompt is not None:
            self.cancelled.wait(0.05)
            while not (self.hold_prompt.is_set() or self.cancelled.is_set()
                       or self._dead.is_set()):
                self.cancelled.wait(0.05)
                self.hold_prompt.wait(0.05)
        sid = msg["params"]["sessionId"]
        self._write({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid,
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": "entendido"}}}})
        reason = "cancelled" if self.cancelled.is_set() else self.stop_reason
        self._write({"jsonrpc": "2.0", "id": rid,
                     "result": {"stopReason": reason}})

    def ask_permission(self, options, tool_call=None):
        rid = self._next_srv_id
        self._next_srv_id += 1
        self._write({"jsonrpc": "2.0", "id": rid,
                     "method": "session/request_permission",
                     "params": {"options": options,
                                "toolCall": tool_call or {"title": "ejecutar comando"}}})
        return self._wait_response(rid)

    def terminate(self):
        self.terminated = True
        try:
            self.stdin.close()
        except OSError:
            pass

    def poll(self):
        return 0 if self._dead.is_set() else None


def make_worker(env, group="local", day="2026-01-01", agent=None, stdin_lines=None,
                elevated=True, today="2026-01-01", store=None, agent_factory=None,
                commit_fn=None):
    store = store or bridge.StateStore(env.state_root / group)
    out = io.StringIO()
    q = queue.Queue()
    for line in (stdin_lines if stdin_lines is not None else []):
        q.put(line)
    q.put(None)
    proc_holder = {}

    def spawn(exe, mode, cwd):
        proc = agent_factory() if agent_factory is not None \
            else (agent or FakeAcpProc())
        proc.spawn_env_mode = mode
        proc.spawn_cwd = cwd
        proc_holder["proc"] = proc
        return proc

    def elev_check(cfg):
        if cfg["elevated"] != elevated:
            raise bridge.ElevationMismatch("mismatch")

    w = bridge.ConsoleWorker(
        env.policy, group, day, store, spawn_agent=spawn,
        stdin_queue=q, stdout=out, poll_state_seconds=0.05,
        today=lambda: today,
        verify_elevation_fn=elev_check,
        mutex_factory=lambda n: bridge.HeldMutex(
            "Local\\T-" + os.urandom(6).hex()),
        auth_fn=lambda e: True,
        title_fn=lambda t: None,
        **({"commit_fn": commit_fn} if commit_fn is not None else {}))
    return w, store, out, proc_holder


def test_initialize_from_now_no_historical_replay(env):
    add_occurrence(env.db, level="ERROR")
    add_occurrence(env.db, level="CRITICAL")
    w, store, spawns = make_watcher(env)
    res = w.poll_once()
    assert res["status"] == "initialized" and res["queued"] == 0
    assert res["cursor"] == 2
    assert spawns == []
    assert store.load()["pending"] == []


def test_no_eligible_event_no_window(env):
    w, store, spawns = make_watcher(env)
    w.poll_once()
    add_occurrence(env.db, level="INFO")
    w.poll_once()
    assert spawns == []
    assert store.load()["cursor"] == 1


def test_route_levels_and_groups(env):
    p = env.policy
    r = bridge.route_event
    assert r(p, {"level": "WARNING", "source": "frontend", "service_id": "sofia_frontend"}) == "local"
    assert r(p, {"level": "CRITICAL", "source": "active", "service_id": "vision_backend"}) == "local"
    assert r(p, {"level": "ERROR", "source": "docker", "service_id": "anything"}) == "elenatec"
    assert r(p, {"level": "ERROR", "source": "test", "service_id": "tec"}) == "elenatec"
    assert r(p, {"level": "ERROR", "source": "x", "service_id": "droplet-1"}) == "elenatec"
    assert r(p, {"level": "ERROR", "source": "x", "service_id": "technology"}) == "local"
    assert r(p, {"level": "INFO", "source": "docker", "service_id": "tec"}) is None


def test_route_monitor_component_stays_local(env):
    """droplet-ssh (poll del monitor docker de Sofia) es area local: el fix
    es Sofia-side aunque la fuente sea docker y el prefijo sea droplet-."""
    p = env.policy
    r = bridge.route_event
    assert r(p, {"level": "ERROR", "source": "docker", "service_id": "droplet-ssh"}) == "local"
    assert r(p, {"level": "WARNING", "source": "docker", "service_id": "droplet-ssh"}) == "local"
    assert r(p, {"level": "ERROR", "source": "docker", "service_id": "droplet-other"}) == "elenatec"


def test_same_day_events_one_spawn(env):
    w, store, spawns = make_watcher(env)
    w.poll_once()
    add_occurrence(env.db, level="ERROR", message="uno")
    add_occurrence(env.db, level="WARNING", message="dos")
    w.poll_once()
    assert len(spawns) == 1 and spawns[0] == ("local", "2026-01-01")
    pending = store.load()["pending"]
    assert len(pending) == 2 and pending[0]["message"] == "uno"
    assert all(e["received_day"] == "2026-01-01" for e in pending)


def test_batch_hold_waits_and_accumulates_burst(env):
    """Con batch_hold_seconds=60 el lote no sale al primer aviso: espera la
    ventana para juntar toda la rafaga en un solo reporte."""
    env.policy["batch_hold_seconds"] = 60
    clock = {"t": 1000.0}
    w, store, spawns = make_watcher(env, now=lambda: clock["t"])
    w.poll_once()
    add_occurrence(env.db, level="ERROR", message="uno")
    w.poll_once()
    assert spawns == []
    assert store.load()["hold_until"] == 1060.0
    clock["t"] = 1030.0
    add_occurrence(env.db, level="ERROR", message="dos")
    w.poll_once()
    assert spawns == []
    assert len(store.load()["pending"]) == 2
    clock["t"] = 1060.0
    res = w.poll_once()
    assert res["queued"] == 0
    assert spawns == [("local", "2026-01-01")]


def test_batch_hold_expires_once_not_extended(env):
    """La ventana no se extiende con cada aviso nuevo: vence 60 s despues del
    primer evento (latencia acotada aunque sigan llegando avisos)."""
    env.policy["batch_hold_seconds"] = 60
    clock = {"t": 1000.0}
    w, store, spawns = make_watcher(env, now=lambda: clock["t"])
    w.poll_once()
    add_occurrence(env.db, level="ERROR", message="uno")
    w.poll_once()
    clock["t"] = 1055.0
    add_occurrence(env.db, level="ERROR", message="dos")
    w.poll_once()
    assert store.load()["hold_until"] == 1060.0
    clock["t"] = 1060.0
    w.poll_once()
    assert spawns == [("local", "2026-01-01")]


def test_batch_hold_disabled_spawns_immediately(env):
    w, store, spawns = make_watcher(env)
    w.poll_once()
    add_occurrence(env.db, level="ERROR")
    w.poll_once()
    assert len(spawns) == 1
    assert store.load()["hold_until"] == 0.0


def test_dedup_same_issue_in_window(env):
    """Ocurrencias del mismo issue se agrupan: un solo reporte con contador."""
    w, store, _ = make_watcher(env)
    w.poll_once()
    issue_id, _ = add_occurrence(env.db, level="ERROR", message="mismo")
    add_occurrence_for_issue(env.db, issue_id)
    add_occurrence_for_issue(env.db, issue_id)
    add_occurrence(env.db, level="ERROR", message="otro")
    res = w.poll_once()
    assert res["queued"] == 2 and res["deduped"] == 2
    pending = store.load()["pending"]
    assert len(pending) == 2
    grouped = [e for e in pending if e["message"] == "mismo"][0]
    assert grouped["duplicates"] == 2
    text = bridge.render_report(pending, "2026-01-01")
    assert "(+2 ocurrencia(s) del mismo issue" in text


def test_dedup_after_dispatch_requeues(env):
    """La deduplicacion es solo dentro de la cola: un issue ya despachado que
    vuelve a fallar se encola de nuevo (no se oculta una recurrencia)."""
    w, store, _ = make_watcher(env)
    w.poll_once()
    issue_id, occ1 = add_occurrence(env.db, level="ERROR", message="mismo")
    w.poll_once()
    with store.transaction() as st:
        st["completed"] = [occ1]
        st["pending"] = []
    add_occurrence_for_issue(env.db, issue_id)
    res = w.poll_once()
    assert res["queued"] == 1 and res["deduped"] == 0
    assert len(store.load()["pending"]) == 1


def test_groups_isolated_cursors(env):
    wl, sl, _ = make_watcher(env, "local")
    we, se, _ = make_watcher(env, "elenatec")
    wl.poll_once(); we.poll_once()
    add_occurrence(env.db, level="ERROR", source="docker", service_id="tec")
    add_occurrence(env.db, level="ERROR", source="frontend", service_id="sofia")
    wl.poll_once(); we.poll_once()
    assert [e["source"] for e in sl.load()["pending"]] == ["frontend"]
    assert [e["source"] for e in se.load()["pending"]] == ["docker"]


def test_readonly_source_unchanged(env):
    add_occurrence(env.db, level="ERROR")
    before = env.db.read_bytes()
    w, _, _ = make_watcher(env)
    w.poll_once(); w.poll_once()
    assert env.db.read_bytes() == before


def test_source_reset_vs_retention_purge(env):
    w, store, _ = make_watcher(env)
    add_occurrence(env.db, level="ERROR")
    w.poll_once()
    add_occurrence(env.db, level="ERROR")
    w.poll_once()
    assert store.load()["cursor"] == 2
    conn = sqlite3.connect(str(env.db))
    conn.execute("DELETE FROM occurrences")
    conn.commit(); conn.close()
    assert w.poll_once()["status"] != "source_reset"
    assert store.load()["cursor"] == 2
    db2 = env.tmp / "sofia2.db"
    make_db(db2)
    add_occurrence(db2, level="ERROR")
    src2 = bridge.OccurrenceSource(
        db2, env.policy["_occurrences_sql"], env.policy["_highwater_sql"])
    w3 = bridge.Watcher(env.policy, "local", src2, store,
                        spawn_worker=lambda *a: FakeChild(),
                        is_worker_alive=lambda g: True, now=lambda: 0.0)
    assert w3.poll_once()["status"] == "source_reset"
    st = store.load()
    assert st["source_reset"] is True and st["cursor"] == 2


def test_failed_queue_write_keeps_cursor(env, monkeypatch):
    w, store, _ = make_watcher(env)
    w.poll_once()
    add_occurrence(env.db, level="ERROR")
    monkeypatch.setattr(store, "_save",
                        lambda s: (_ for _ in ()).throw(OSError("disco lleno")))
    with pytest.raises(OSError):
        w.poll_once()
    monkeypatch.undo()
    st = store.load()
    assert st["cursor"] == 0 and st["pending"] == []


def test_state_save_retries_transient_winerror(env, monkeypatch):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["cursor"] = 1
    real_replace = os.replace
    monkeypatch.setattr(bridge.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            exc = PermissionError(13, "Access is denied")
            exc.winerror = 5
            raise exc
        return real_replace(src, dst)

    monkeypatch.setattr(bridge.os, "replace", flaky_replace)
    with store.transaction() as st:
        st["cursor"] = 2
    assert calls["n"] == 3
    assert store.load()["cursor"] == 2
    assert not list(store.state_dir.glob(".state-*.tmp"))


def test_state_save_fails_closed_after_retries(env, monkeypatch):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["cursor"] = 1
    monkeypatch.setattr(bridge.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def denied_replace(src, dst):
        calls["n"] += 1
        exc = PermissionError(13, "Access is denied")
        exc.winerror = 5
        raise exc

    monkeypatch.setattr(bridge.os, "replace", denied_replace)
    with pytest.raises(PermissionError):
        with store.transaction() as st:
            st["cursor"] = 2
    assert calls["n"] == bridge.STATE_REPLACE_ATTEMPTS
    assert not list(store.state_dir.glob(".state-*.tmp"))
    assert store.load()["cursor"] == 1


def test_state_save_backoff_grows_and_is_capped(env, monkeypatch):
    """El backoff ante WinError 5/32 crece exponencialmente y queda topado:
    locks transitorios (AV/indexer) de varios segundos ya no abortan el ciclo."""
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["cursor"] = 1
    sleeps = []
    monkeypatch.setattr(bridge.time, "sleep", lambda s: sleeps.append(s))

    def denied_replace(src, dst):
        exc = PermissionError(13, "Access is denied")
        exc.winerror = 5
        raise exc

    monkeypatch.setattr(bridge.os, "replace", denied_replace)
    with pytest.raises(PermissionError):
        with store.transaction() as st:
            st["cursor"] = 2
    assert len(sleeps) == bridge.STATE_REPLACE_ATTEMPTS - 1
    assert sleeps[0] == bridge.STATE_REPLACE_DELAY
    assert sleeps[-1] == bridge.STATE_REPLACE_MAX_DELAY
    assert all(s <= bridge.STATE_REPLACE_MAX_DELAY for s in sleeps)
    assert sum(sleeps) >= 5.0  # aguanta locks de varios segundos, no solo ~2 s


def test_malformed_state_fail_closed(env):
    w, store, _ = make_watcher(env)
    store.state_dir.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(bridge.StateError):
        w.poll_once()
    store.path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(bridge.StateError):
        w.poll_once()


def test_state_without_hold_until_loads(env):
    """Un state.json previo a la ventana de acumulacion (sin hold_until) se
    carga sin romper: el campo nuevo se completa con su default."""
    store = bridge.StateStore(env.state_root / "local")
    store.state_dir.mkdir(parents=True)
    state = bridge._initial_state()
    state.pop("hold_until")
    store.path.write_text(json.dumps(state), encoding="utf-8")
    assert store.load()["hold_until"] == 0.0


def test_state_transaction_serializes_rmw(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["cursor"] = 5
        st["pending"] = [ev(5)]
    barrier = threading.Barrier(2)
    done = []

    def worker_write():
        barrier.wait()
        with store.transaction() as st:
            st["sessions"]["2026-01-01"] = {"session_id": "sid-1",
                                            "initial_prompt_sent": True}
            st["pending"] = [e for e in st["pending"]
                             if e["occurrence_id"] != 5]
            st["completed"] = sorted(set(st["completed"]) | {5})
        done.append("w")

    def watcher_write():
        barrier.wait()
        with store.transaction() as st:
            st["cursor"] = 9
            st["pending"].append(ev(9))
        done.append("x")

    t1 = threading.Thread(target=worker_write)
    t2 = threading.Thread(target=watcher_write)
    t1.start(); t2.start(); t1.join(10); t2.join(10)
    assert len(done) == 2
    st = store.load()
    assert st["cursor"] == 9
    assert st["sessions"]["2026-01-01"]["session_id"] == "sid-1"
    ids = [e["occurrence_id"] for e in st["pending"]]
    assert ids == [9]
    assert st["completed"] == [5]


@pytest.mark.skipif(os.name != "nt", reason="mutex Windows")
def test_state_transaction_multiprocess(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["cursor"] = 0
    script_file = env.tmp / "tx_child.py"
    script_file.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{REPO_ROOT / 'backend'}')\n"
        "from pathlib import Path\n"
        "from app.scripts.sofia_devin_bridge import StateStore\n"
        f"s = StateStore(Path(r'{env.state_root / 'local'}'))\n"
        "with s.transaction() as st:\n"
        "    st['cursor'] = st['cursor'] + 1\n",
        encoding="utf-8")
    procs = [subprocess.Popen(
        [sys.executable, str(script_file)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(3)]
    for p in procs:
        p.wait(30)
        assert p.returncode == 0
    assert store.load()["cursor"] == 3


def test_pending_full_exactly_once(env):
    w, store, _ = make_watcher(env)
    w.poll_once()
    with store.transaction() as st:
        st["pending"] = [ev(10_000 + i) for i in range(bridge.MAX_PENDING)]
    for i in range(60):
        add_occurrence(env.db, level="ERROR", message=f"e{i}")
    res = w.poll_once()
    st = store.load()
    assert res["queued"] == 0
    assert st["status"] == "pending_full"
    assert st["cursor"] == 0
    with store.transaction() as st:
        st["pending"] = st["pending"][:100]
    res = w.poll_once()
    st = store.load()
    assert res["queued"] == 50
    assert st["cursor"] == 50
    with store.transaction() as st:
        st["pending"] = []
    w.poll_once()
    st = store.load()
    assert st["cursor"] == 60
    assert len([e for e in st["pending"]]) == 10
    assert sorted(e["occurrence_id"] for e in st["pending"]) == list(range(51, 61))


def test_rollover_day_routing(env):
    clock = {"day": "2026-01-01"}
    w, store, spawns = make_watcher(env, today=lambda: clock["day"])
    w.poll_once()
    add_occurrence(env.db, level="ERROR", message="ayer")
    w.poll_once()
    assert spawns == [("local", "2026-01-01")]
    clock["day"] = "2026-01-02"
    res = w.poll_once()
    assert len(spawns) == 1
    add_occurrence(env.db, level="ERROR", message="hoy")
    w.poll_once()
    st = store.load()
    days = {e["received_day"] for e in st["pending"]}
    assert days == {"2026-01-01", "2026-01-02"}
    assert spawns[-1] == ("local", "2026-01-01")


def test_abandoned_inflight_marked_uncertain(env):
    w, store, spawns = make_watcher(env)
    with store.transaction() as st:
        st["initialized"] = True
        st["cursor"] = 1
        st["pending"] = [ev(1)]
        st["inflight"] = {"event_ids": [1], "prompt_sent": True,
                          "uncertain": False, "use_initial": True,
                          "day": "2026-01-01", "stop_reason": None}
    w.poll_once()
    st = store.load()
    assert st["uncertain"] is True and st["inflight"]["uncertain"] is True


def test_spawn_failure_backoff_and_circuit(env):
    calls = []
    now = {"t": 1000.0}

    def fail_spawn(p, g, d):
        raise OSError("no console")

    w, store, _ = make_watcher(
        env, spawn_worker=fail_spawn, now=lambda: now["t"])
    w.poll_once()
    add_occurrence(env.db, level="ERROR")
    w.poll_once()
    assert store.load()["spawn_failures"] == 1
    w.poll_once()
    assert store.load()["spawn_failures"] == 1
    now["t"] += 40
    w.poll_once()
    assert store.load()["spawn_failures"] == 2
    with store.transaction() as s2:
        s2["spawn_failures"] = bridge.MAX_SPAWN_FAILURES - 1
        s2["last_spawn"] = now["t"] - 100
        s2["worker_ready"] = False
        s2["spawn_counted"] = False
        s2["status"] = "ok"
        s2["status_at"] = now["t"]
        s2["next_retry_after"] = 0
    w.poll_once()
    assert store.load()["circuit_open"] is True
    now["t"] += 10_000
    w.poll_once()
    assert store.load()["spawn_failures"] == bridge.MAX_SPAWN_FAILURES


def test_grace_lease_no_duplicate_spawn(env):
    child = FakeChild(rc=None)
    alive = {"v": False}
    now = {"t": 5000.0}
    w, store, spawns = make_watcher(
        env, spawn_worker=lambda p, g, d: spawns.append((g, d)) or child,
        is_worker_alive=lambda g: alive["v"], now=lambda: now["t"])
    w.poll_once()
    add_occurrence(env.db, level="ERROR")
    w.poll_once()
    assert len(spawns) == 1
    w.poll_once()
    assert len(spawns) == 1
    now["t"] += 100
    w.poll_once()
    assert len(spawns) == 1
    alive["v"] = True
    now["t"] += 100
    w.poll_once()
    assert len(spawns) == 1


def test_worker_death_between_polls_pauses(env):
    now = {"t": 8000.0}
    w, store, spawns = make_watcher(
        env,
        spawn_worker=lambda p, g, d: spawns.append((g, d)) or FakeChild(rc=1),
        is_worker_alive=lambda g: False, now=lambda: now["t"])
    w.poll_once()
    add_occurrence(env.db, level="ERROR")
    w.poll_once()
    assert len(spawns) == 1
    for _ in range(bridge.MAX_SPAWN_FAILURES * 3):
        w.poll_once()
        now["t"] += 400
    st = store.load()
    assert st["circuit_open"] is True
    assert st["spawn_failures"] == bridge.MAX_SPAWN_FAILURES
    assert len(spawns) == bridge.MAX_SPAWN_FAILURES
    now["t"] += 10_000
    w.poll_once()
    assert len(spawns) == bridge.MAX_SPAWN_FAILURES


def test_pause_until_new_report(env):
    w, store, spawns = make_watcher(
        env, spawn_worker=lambda p, g, d: spawns.append((g, d)) or FakeChild(rc=0))
    w.poll_once()
    add_occurrence(env.db, level="ERROR")
    w.poll_once()
    assert len(spawns) == 1
    with store.transaction() as st:
        st["paused"] = True
    w.poll_once()
    assert len(spawns) == 1
    add_occurrence(env.db, level="CRITICAL", message="nuevo")
    w.poll_once()
    assert store.load()["paused"] is False
    assert len(spawns) == 2


def test_missing_devin_exe_fail_closed(env):
    env.devin.unlink()
    w, store, spawns = make_watcher(env)
    w.poll_once()
    add_occurrence(env.db, level="ERROR")
    w.poll_once()
    assert spawns == []
    assert store.load()["status"] == "agent_missing"


@pytest.mark.skipif(os.name != "nt", reason="mutex Windows")
def test_held_and_lock_mutex_windows(env):
    name = "Local\\SDBTest-" + os.urandom(6).hex()
    m1, m2 = bridge.HeldMutex(name), bridge.HeldMutex(name)
    assert m1.acquire() and not m2.acquire()
    m1.release()
    assert m2.acquire()
    m2.release()
    lname = "Local\\SDBLock-" + os.urandom(6).hex()
    l1 = bridge.LockMutex(lname)
    assert l1.acquire(1000)
    script_file = env.tmp / "lock_child.py"
    script_file.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{REPO_ROOT / 'backend'}')\n"
        "from app.scripts.sofia_devin_bridge import LockMutex\n"
        f"m = LockMutex(r'{lname}')\n"
        "ok = m.acquire(500)\n"
        "m.release(); m.close()\n"
        "sys.exit(0 if ok else 7)\n",
        encoding="utf-8")
    p = subprocess.run([sys.executable, str(script_file)],
                       capture_output=True, timeout=30)
    assert p.returncode == 7
    l1.release(); l1.close()
    p2 = subprocess.run([sys.executable, str(script_file)],
                        capture_output=True, timeout=30)
    assert p2.returncode == 0


@pytest.mark.skipif(os.name != "nt", reason="mutex Windows")
def test_worker_alive_detection(env):
    group = "t-walive-" + os.urandom(4).hex()
    name = bridge.worker_mutex_name(group)
    assert bridge.exists_mutex(name) is False
    m = bridge.HeldMutex(name)
    m.acquire()
    assert bridge.worker_alive(group) is True
    m.release()
    assert bridge.worker_alive(group) is False


def test_watcher_single_instance(env, monkeypatch):
    name = "Local\\SDBTestWatcher-" + os.urandom(4).hex()
    monkeypatch.setattr(bridge, "watcher_mutex_name", lambda g: name)
    m = bridge.HeldMutex(name)
    assert m.acquire()
    try:
        rc = bridge.main(["watch", "--group", "local", "--once",
                          "--policy-dir", str(env.policy_dir)])
        assert rc == 4
    finally:
        m.release()


def test_redact_bearer_and_secret():
    out = bridge.redact_text("Authorization: Bearer abc-secret")
    assert "abc-secret" not in out
    out2 = bridge.redact_text('api_key="zzz999" password: hunter2')
    assert "zzz999" not in out2 and "hunter2" not in out2


def test_redact_overlapping_token_paths():
    secret = "THIS_IS_ONLY_A_TEST_TOKEN"
    out = bridge.redact_text(f"/api/hikvision/events/{secret}")
    assert secret not in out
    assert "/api/" in out
    out2 = bridge.redact_text(f"/api/[REDACTADO]/events/{secret}")
    assert secret not in out2
    assert "[REDACTADO]" in out2


def test_redact_url_pem_esc():
    out = bridge.redact_text("GET https://h.example/x?token=AAA&ok=1 rtsp://u:p@cam/1")
    assert "token=AAA" not in out and "u:p@" not in out
    pem = "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----"
    assert "ABC" not in bridge.redact_text(pem)
    out = bridge.redact_text("hola\x1b[31m rojo\x1b[0m \x07 fin")
    assert "\x1b" not in out and "\x07" not in out and "rojo" in out


def test_event_redacted_on_enqueue(env):
    w, store, _ = make_watcher(env)
    w.poll_once()
    add_occurrence(env.db, level="ERROR",
                   message="fallo https://x/?token=SECRET", detail="Bearer tok123")
    w.poll_once()
    blob = json.dumps(store.load()["pending"][0])
    assert "SECRET" not in blob and "tok123" not in blob


def test_worker_sends_initial_batch(env):
    w, store, spawns = make_watcher(env)
    w.poll_once()
    add_occurrence(env.db, level="ERROR", message="primero")
    w.poll_once()
    agent = FakeAcpProc()
    worker, wstore, out, ph = make_worker(env, agent=agent)
    assert worker.run() == 0
    methods = [m["method"] for m in agent.received]
    assert methods.count("session/new") == 1
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 1
    text = prompts[0]["params"]["prompt"][0]["text"]
    assert "Sofía" in text or "Sofia" in text
    assert "primero" in text
    st = wstore.load()
    assert st["pending"] == [] and st["completed"] == [1]
    assert st["sessions"]["2026-01-01"]["initial_prompt_sent"] is True
    assert st["worker_ready"] is True
    assert agent.spawn_env_mode == env.policy["permission_mode"]
    assert agent.spawn_cwd == env.policy["groups"]["local"]["cwd"]


def test_worker_waits_for_hold_window(env):
    """El worker con consola abierta tambien respeta la ventana: no manda el
    lote hasta que vence el hold (o hasta que se limpia)."""
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["cursor"] = 1
        st["pending"] = [ev(1, message="en espera")]
        st["hold_until"] = time.time() + 3600
    agent = FakeAcpProc()
    q = queue.Queue()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q,
        stdout=io.StringIO(), poll_state_seconds=0.05,
        today=lambda: "2026-01-01",
        verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex(
            "Local\\T5-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    time.sleep(0.5)
    assert not any(m["method"] == "session/prompt" for m in agent.received)
    with store.transaction() as st:
        st["hold_until"] = 0.0
    for _ in range(200):
        if any(m["method"] == "session/prompt" for m in agent.received):
            break
        time.sleep(0.05)
    q.put(None)
    t.join(15)
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 1
    assert "en espera" in prompts[0]["params"]["prompt"][0]["text"]
    assert store.load()["completed"] == [1]


def test_worker_second_batch_followup(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["cursor"] = 2
        st["pending"] = [ev(1, message="lento"), ev(2, message="segundo")]
    hold = threading.Event()
    agent = FakeAcpProc(hold_prompt=hold)
    q = queue.Queue()
    out = io.StringIO()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q, stdout=out,
        poll_state_seconds=0.05, today=lambda: "2026-01-01",
        verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex("Local\\T2-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    for _ in range(200):
        if any(m["method"] == "session/prompt" for m in agent.received):
            break
        time.sleep(0.05)
    hold.set()
    for _ in range(200):
        if store.load()["completed"] == [1, 2]:
            break
        time.sleep(0.05)
    q.put(None)
    t.join(15)
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 1
    assert "lento" in prompts[0]["params"]["prompt"][0]["text"]
    assert "segundo" in prompts[0]["params"]["prompt"][0]["text"]
    assert store.load()["completed"] == [1, 2]


def test_worker_skips_other_day_events(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1, day="2026-01-01"), ev(2, day="2026-01-02")]
    agent = FakeAcpProc()
    worker, wstore, out, ph = make_worker(env, agent=agent, day="2026-01-01")
    worker.run()
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 1
    text = prompts[0]["params"]["prompt"][0]["text"]
    assert "occurrence 1" in text and "occurrence 2" not in text
    st = store.load()
    assert [e["occurrence_id"] for e in st["pending"]] == [2]


def test_worker_exits_on_day_rollover(env):
    clock = {"day": "2026-01-01"}
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1, day="2026-01-01", message="ayer")]
    agent = FakeAcpProc()
    q = queue.Queue()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q, stdout=io.StringIO(),
        poll_state_seconds=0.05, today=lambda: clock["day"],
        verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex("Local\\T3-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    for _ in range(200):
        if any(m["method"] == "session/prompt" for m in agent.received):
            break
        time.sleep(0.05)
    for _ in range(200):
        if not store.load()["pending"]:
            break
        time.sleep(0.05)
    clock["day"] = "2026-01-02"
    t.join(15)
    assert not t.is_alive()
    sid1 = store.load()["sessions"]["2026-01-01"]["session_id"]
    assert sid1 == "sess-1"


def test_rollover_new_day_new_session(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(5, day="2026-01-02", message="hoy")]
        st["sessions"]["2026-01-01"] = {"session_id": "sess-viejo",
                                        "initial_prompt_sent": True}
    agent = FakeAcpProc()
    worker, _, out, _ = make_worker(env, agent=agent, day="2026-01-02",
                                    today="2026-01-02", store=store)
    worker.run()
    news = [m for m in agent.received if m["method"] == "session/new"]
    loads = [m for m in agent.received if m["method"] == "session/load"]
    assert len(news) == 1 and loads == []
    st = store.load()
    assert st["sessions"]["2026-01-02"]["session_id"] == "sess-1"
    assert st["sessions"]["2026-01-01"]["session_id"] == "sess-viejo"


def test_uncertain_blocks_auto_sends_and_recovers(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1, message="incierto"), ev(2, message="posterior")]
        st["inflight"] = {"event_ids": [1], "prompt_sent": True,
                          "uncertain": True, "use_initial": True,
                          "day": "2026-01-01", "stop_reason": None}
        st["uncertain"] = True
    agent = FakeAcpProc()
    worker, wstore, out, ph = make_worker(env, agent=agent,
                                          stdin_lines=["/reintentar"])
    worker.run()
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 2
    text = prompts[0]["params"]["prompt"][0]["text"]
    assert "incierto" in text and "posterior" not in text
    assert "Sofía" in text or "sesion de sofia" in text.lower()
    text2 = prompts[1]["params"]["prompt"][0]["text"]
    assert "posterior" in text2 and "incierto" not in text2
    assert "sesion de sofia" not in text2.lower()
    st = store.load()
    assert st["uncertain"] is False
    assert st["completed"] == [1, 2]
    assert st["pending"] == []


def test_uncertain_retry_reconnects_same_session(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1, message="incierto")]
        st["inflight"] = {"event_ids": [1], "prompt_sent": True,
                          "uncertain": True, "use_initial": False,
                          "day": "2026-01-01", "stop_reason": None}
        st["uncertain"] = True
        st["sessions"]["2026-01-01"] = {"session_id": "sess-77",
                                        "initial_prompt_sent": True}
    agent1 = FakeAcpProc()
    agent2 = FakeAcpProc()
    agents = iter([agent1, agent2])
    q = queue.Queue()
    out = io.StringIO()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: next(agents),
        stdin_queue=q, stdout=io.StringIO(), poll_state_seconds=0.05,
        today=lambda: "2026-01-01", verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex("Local\\T4-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    for _ in range(200):
        if any(m["method"] == "session/load" for m in agent1.received):
            break
        time.sleep(0.05)
    agent1.terminate()
    time.sleep(0.3)
    q.put("/reintentar")
    q.put(None)
    t.join(20)
    assert not t.is_alive()
    loads = [m for m in agent2.received if m["method"] == "session/load"]
    assert loads and loads[0]["params"]["sessionId"] == "sess-77"
    news = [m for m in agent2.received if m["method"] == "session/new"]
    assert news == []
    prompts = [m for m in agent2.received if m["method"] == "session/prompt"]
    assert prompts and "incierto" in prompts[0]["params"]["prompt"][0]["text"]


def test_stopreason_not_end_turn_never_acks(env):
    for reason in ("cancelled", "max_tokens", "refusal", None):
        st_dir = env.state_root / f"local-{reason}"
        store = bridge.StateStore(st_dir)
        with store.transaction() as st:
            st["initialized"] = True
            st["pending"] = [ev(1)]
        agent = FakeAcpProc(stop_reason=reason)
        worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
        worker.run()
        st = store.load()
        assert st["completed"] == []
        assert st["pending"] and st["inflight"] is not None
        assert st["uncertain"] is True
        assert st["status"].startswith("turn_incomplete")
        assert st["sessions"]["2026-01-01"]["initial_prompt_sent"] is False


def test_protocol_version_mismatch_fails(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    agent = FakeAcpProc(init_protocol=99)
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    assert worker.run() == 5
    assert wstore.load()["status"] == "agent_init_error"


def test_permission_payload_and_stale_answers(env):
    answered = []
    opts = [{"optionId": "allow-once", "name": "Permitir", "kind": "allow_once"},
            {"optionId": "reject", "name": "Rechazar", "kind": "reject_once"}]
    calls = {"n": 0}

    def hook(msg, agent):
        calls["n"] += 1
        answered.append(agent.ask_permission(opts))
        if calls["n"] == 1:
            answered.append(agent.ask_permission(opts))

    agent = FakeAcpProc(prompt_hook=hook)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, stdin_lines=[])

    def answer_first():
        worker._permission_pending.wait(10)
        worker.stdin_queue.put("1")

    threading.Thread(target=answer_first, daemon=True).start()
    worker.run()
    first = (answered[0].get("result") or {}).get("outcome", {})
    second = (answered[1].get("result") or {}).get("outcome", {})
    assert first == {"outcome": "selected", "optionId": "allow-once"}
    assert second.get("outcome") == "cancelled" or \
        second.get("optionId") == "reject"


def test_permission_eof_defaults_cancel(env):
    answered = {}
    opts = [{"optionId": "allow-once", "name": "Permitir", "kind": "allow_once"}]

    def hook(msg, agent):
        answered["r"] = agent.ask_permission(opts)

    agent = FakeAcpProc(prompt_hook=hook)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, _, out, _ = make_worker(env, agent=agent, stdin_lines=[])
    worker.run()
    outcome = (answered["r"].get("result") or {}).get("outcome")
    assert outcome == {"outcome": "cancelled"}


def test_salir_during_slow_turn(env):
    hold = threading.Event()
    agent = FakeAcpProc(hold_prompt=hold)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    q = queue.Queue()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q, stdout=io.StringIO(),
        poll_state_seconds=0.05, today=lambda: "2026-01-01",
        verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex("Local\\T5-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    for _ in range(200):
        if any(m["method"] == "session/prompt" for m in agent.received):
            break
        time.sleep(0.05)
    q.put("/salir")
    for _ in range(200):
        if any(m["method"] == "session/cancel" for m in agent.received):
            break
        time.sleep(0.05)
    assert any(m["method"] == "session/cancel" for m in agent.received)
    hold.set()
    t.join(15)
    assert not t.is_alive()


def test_resume_exact_session_id(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["sessions"]["2026-01-01"] = {"session_id": "sess-previa",
                                        "initial_prompt_sent": True}
    agent = FakeAcpProc(load_session=True)
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    worker.run()
    methods = [m["method"] for m in agent.received]
    assert "session/load" in methods and "session/new" not in methods
    load = [m for m in agent.received if m["method"] == "session/load"][0]
    assert load["params"]["sessionId"] == "sess-previa"


def test_load_failure_no_replacement_session(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["sessions"]["2026-01-01"] = {"session_id": "sess-previa",
                                        "initial_prompt_sent": True}
    agent = FakeAcpProc(load_error="sesion expirada")
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    assert worker.run() == 5
    assert "session/new" not in [m["method"] for m in agent.received]
    assert wstore.load()["status"] == "manual_gap"


def test_elevation_mismatch_fail_closed(env):
    worker, wstore, out, ph = make_worker(env, elevated=False)
    assert worker.run() == 3
    assert wstore.load()["status"] == "elevation_mismatch"


def test_agent_missing_and_init_error(env):
    env.devin.unlink()
    store = bridge.StateStore(env.state_root / "local")
    worker, wstore, out, ph = make_worker(env, store=store)
    assert worker.run() == 5
    assert wstore.load()["status"] == "agent_missing"

    env.devin.write_bytes(b"fake")
    store2 = bridge.StateStore(env.state_root / "local2")
    with store2.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]

    def spawn(exe, m, c):
        return FakeAcpProc(init_error="auth requerida")
    worker2 = bridge.ConsoleWorker(
        env.policy, "local2" if False else "local", "2026-01-01", store2,
        spawn_agent=spawn, stdin_queue=_eof_queue(), stdout=io.StringIO(),
        poll_state_seconds=0.05, today=lambda: "2026-01-01",
        verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex("Local\\T6-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    assert worker2.run() == 5
    assert store2.load()["status"] == "agent_init_error"


def _eof_queue():
    q = queue.Queue()
    q.put(None)
    return q


def test_acp_transport_timeout_and_remote_error():
    proc = FakeAcpProc()
    proc._serve_prompt = lambda msg: None
    transport = bridge.AcpTransport(proc, lambda m: None,
                                  lambda m: (_ for _ in ()).throw(bridge.AcpError("x")))
    transport.request("initialize", {"protocolVersion": 1})
    with pytest.raises(bridge.AcpError, match="timeout"):
        transport.request("session/prompt", {"sessionId": "s", "prompt": []},
                          timeout=0.5)
    with pytest.raises(bridge.AcpRemoteError):
        transport.request("session/nope", {})
    transport.close()
    proc.terminate()


def test_transport_eof_closes_pending():
    proc = FakeAcpProc()
    transport = bridge.AcpTransport(proc, lambda m: None, lambda m: None)
    proc.terminate()
    time.sleep(0.4)
    with pytest.raises(bridge.AcpError):
        transport.request("initialize", {}, timeout=2)


def test_stream_split_secret_and_pem_never_leak(env):
    worker, _, out, _ = make_worker(env)
    worker._stream_write("Authorization: Bea")
    worker._stream_write("rer abc-secret\n")
    worker._flush_stream()
    assert "abc-secret" not in out.getvalue()
    out.truncate(0); out.seek(0)
    worker._stream_write("-----BEGIN PRIVATE KEY-----\n")
    worker._stream_write("ABC\n")
    worker._stream_write("-----END PRIVATE KEY-----\n")
    worker._flush_stream()
    assert "ABC" not in out.getvalue()
    out.truncate(0); out.seek(0)
    worker._stream_write("hola ")
    worker._stream_write("mundo\n")
    assert "hola mundo" in out.getvalue()


def test_cancel_is_notification_without_id():
    proc = FakeAcpProc()
    transport = bridge.AcpTransport(proc, lambda m: None, lambda m: None)
    transport.request("initialize", {"protocolVersion": 1})
    session = bridge.AcpSession(transport)
    session.cancel("sess-1")
    deadline = time.time() + 5
    cancels = []
    while time.time() < deadline:
        cancels = [m for m in proc.received if m["method"] == "session/cancel"]
        if cancels:
            break
        time.sleep(0.05)
    transport.close()
    proc.terminate()
    assert cancels and "id" not in cancels[0]
    assert cancels[0]["params"]["sessionId"] == "sess-1"


def test_salir_during_permission_exits_bounded(env):
    opts = [{"optionId": "allow-once", "name": "Permitir", "kind": "allow_once"},
            {"optionId": "reject", "name": "Rechazar", "kind": "reject_once"}]
    hold = threading.Event()

    def hook(msg, agent):
        agent.ask_permission(opts)

    agent = FakeAcpProc(hold_prompt=hold, prompt_hook=hook)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    q = queue.Queue()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q,
        stdout=io.StringIO(), poll_state_seconds=0.05,
        today=lambda: "2026-01-01", verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex(
            "Local\\T-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    worker.cancel_grace = 0.5
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    assert worker._permission_pending.wait(10)
    q.put("/salir")
    deadline = time.time() + 10
    while time.time() < deadline and not any(
            m["method"] == "session/cancel" for m in agent.received):
        time.sleep(0.05)
    assert any(m["method"] == "session/cancel" for m in agent.received)
    t.join(15)
    assert not t.is_alive()


def test_eof_during_active_turn_cancels(env):
    hold = threading.Event()
    agent = FakeAcpProc(hold_prompt=hold)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    q = queue.Queue()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q,
        stdout=io.StringIO(), poll_state_seconds=0.05,
        today=lambda: "2026-01-01", verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex(
            "Local\\T-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    worker.cancel_grace = 0.5
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    for _ in range(200):
        if any(m["method"] == "session/prompt" for m in agent.received):
            break
        time.sleep(0.05)
    q.put(None)
    deadline = time.time() + 10
    while time.time() < deadline and not any(
            m["method"] == "session/cancel" for m in agent.received):
        time.sleep(0.05)
    assert any(m["method"] == "session/cancel" for m in agent.received)
    t.join(15)
    assert not t.is_alive()


def test_healthy_worker_close_pauses_until_new_report(env):
    now = {"t": 9000.0}
    alive = {"v": False}
    w, store, spawns = make_watcher(
        env,
        spawn_worker=lambda p, g, d: spawns.append((g, d)) or FakeChild(rc=0),
        is_worker_alive=lambda g: alive["v"], now=lambda: now["t"])
    w.poll_once()
    add_occurrence(env.db, level="ERROR")
    w.poll_once()
    assert len(spawns) == 1
    alive["v"] = True
    with store.transaction() as st:
        st["worker_ready"] = True
    w.poll_once()
    st = store.load()
    assert st["spawn_counted"] is True and st["spawn_failures"] == 0
    alive["v"] = False
    w.poll_once()
    st = store.load()
    assert st["paused"] is True
    w.poll_once()
    w.poll_once()
    assert len(spawns) == 1
    add_occurrence(env.db, level="CRITICAL", message="nuevo")
    w.poll_once()
    assert len(spawns) == 2
    assert store.load()["paused"] is False


def test_fresh_lease_blocks_spawn_after_watcher_restart(env):
    now = {"t": 2000.0}
    w, store, spawns = make_watcher(
        env, is_worker_alive=lambda g: False, now=lambda: now["t"])
    w.poll_once()
    add_occurrence(env.db, level="ERROR")
    w.poll_once()
    assert len(spawns) == 1
    source = bridge.OccurrenceSource(
        env.db, env.policy["_occurrences_sql"], env.policy["_highwater_sql"])
    w2 = bridge.Watcher(env.policy, "local", source, store,
                        spawn_worker=lambda p, g, d:
                            spawns.append((g, d)) or FakeChild(rc=None),
                        is_worker_alive=lambda g: False,
                        now=lambda: now["t"])
    w2.poll_once()
    assert len(spawns) == 1
    now["t"] += 1000
    w2.poll_once()
    assert len(spawns) == 1
    now["t"] += 100
    w2.poll_once()
    assert len(spawns) == 2


def test_spawn_callback_ready_not_overwritten(env):
    w, store, spawns = make_watcher(env)
    w.poll_once()
    add_occurrence(env.db, level="ERROR")

    def spawn_marking_ready(p, g, d):
        spawns.append((g, d))
        with store.transaction() as st:
            st["worker_ready"] = True
        return FakeChild(rc=None)

    w.spawn_worker = spawn_marking_ready
    w.poll_once()
    assert len(spawns) == 1
    assert store.load()["worker_ready"] is True


def test_new_events_do_not_reset_failing_circuit(env):
    now = {"t": 500.0}
    calls = []

    def fail_spawn(p, g, d):
        calls.append(d)
        raise OSError("no console")

    w, store, _ = make_watcher(
        env, spawn_worker=fail_spawn, now=lambda: now["t"])
    w.poll_once()
    add_occurrence(env.db, level="ERROR", message="e0")
    for i in range(bridge.MAX_SPAWN_FAILURES + 3):
        now["t"] += 400
        add_occurrence(env.db, level="ERROR", message=f"e{i+1}")
        w.poll_once()
        if store.load()["circuit_open"]:
            break
    st = store.load()
    assert st["circuit_open"] is True
    assert st["spawn_failures"] == bridge.MAX_SPAWN_FAILURES
    attempts = len(calls)
    now["t"] += 10_000
    add_occurrence(env.db, level="ERROR", message="mas")
    w.poll_once()
    assert store.load()["circuit_open"] is True
    assert len(calls) == attempts


@pytest.mark.skipif(os.name != "nt", reason="mutex Windows")
def test_state_mutex_abandoned_by_killed_child(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["cursor"] = 7
    script_file = env.tmp / "abandon_child.py"
    lock_name = store._lock_name()
    script_file.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, r'{REPO_ROOT / 'backend'}')\n"
        "from app.scripts.sofia_devin_bridge import LockMutex\n"
        f"m = LockMutex(r'{lock_name}')\n"
        "assert m.acquire(5000)\n"
        "print('HELD', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(script_file)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True)
    try:
        assert p.stdout.readline().strip() == "HELD"
    finally:
        p.terminate()
        p.wait(10)
    with store.transaction() as st:
        st["cursor"] = st["cursor"] + 1
    assert store.load()["cursor"] == 8


def test_manual_worker_without_events_no_acp(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
    spawned = []
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: spawned.append(1) or FakeAcpProc(),
        stdin_queue=_eof_queue(), stdout=io.StringIO(),
        poll_state_seconds=0.05, today=lambda: "2026-01-01",
        verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex(
            "Local\\T-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    assert worker.run() == 0
    assert spawned == []
    assert store.load()["sessions"] == {}


def test_protocol_version_none_fails(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    agent = FakeAcpProc(init_protocol=None)
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    assert worker.run() == 5
    assert wstore.load()["status"] == "agent_init_error"


def test_retry_then_later_batch_uses_followup(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1, message="incierto"), ev(2, message="posterior")]
        st["inflight"] = {"event_ids": [1], "prompt_sent": True,
                          "uncertain": True, "use_initial": True,
                          "day": "2026-01-01", "stop_reason": None}
        st["uncertain"] = True
    agent = FakeAcpProc()
    worker, wstore, out, ph = make_worker(env, agent=agent,
                                          stdin_lines=["/reintentar"])
    worker.run()
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 2
    text1 = prompts[0]["params"]["prompt"][0]["text"]
    text2 = prompts[1]["params"]["prompt"][0]["text"]
    assert "apps de esta PC" in text1
    assert "apps de esta PC" not in text2
    assert "posterior" in text2


@pytest.mark.skipif(os.name != "nt", reason="installer PowerShell")
def test_installer_blocks_install_when_auth_fails(tmp_path):
    root = tmp_path / "repo"
    policy_dir = root / ".devin" / "sofia-cli"
    (root / "backend" / "app" / "scripts").mkdir(parents=True)
    policy_dir.mkdir(parents=True)
    (root / "backend" / "app" / "scripts" /
     "sofia_devin_bridge.py").write_text("x", encoding="utf-8")
    for name in ("occurrences.sql", "highwater.sql",
                 "local-initial.txt", "elenatec-initial.txt"):
        (policy_dir / name).write_bytes((AUTH_POLICY_DIR / name).read_bytes())
    (root / "backend").mkdir(exist_ok=True)
    (root / "backend" / "python.exe").write_bytes(b"fake")
    fake_devin = os.environ.get("SystemRoot", r"C:\Windows") + \
        r"\System32\where.exe"
    (root / "sofia.db").write_bytes(b"fake")
    policy = json.loads((AUTH_POLICY_DIR / "policy.json").read_text(
        encoding="utf-8"))
    policy["source_db"] = str(root / "sofia.db")
    policy["state_root"] = str(root / "state")
    policy["devin_executable"] = fake_devin
    policy["python_executable"] = str(root / "backend" / "python.exe")
    (policy_dir / "policy.json").write_text(json.dumps(policy),
                                          encoding="utf-8")
    script = REPO_ROOT / ".devin" / "sofia-cli" / "install.ps1"
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(script), "-Install", "-PolicyDir", str(policy_dir)],
        capture_output=True, text=True, timeout=120)
    assert r.returncode != 0
    assert "autenticado" in (r.stdout + r.stderr).lower()


def test_stream_pem_tail_without_end_line(env):
    worker, _, out, _ = make_worker(env)
    worker._stream_write("-----BEGIN PRIVATE KEY-----\n")
    worker._stream_write("ABC")
    worker._flush_stream()
    assert "ABC" not in out.getvalue()


def test_stream_pem_begin_inside_partial_tail(env):
    worker, _, out, _ = make_worker(env)
    worker._stream_write("nota previa\n-----BEGIN PRIVATE KEY-----ABC")
    worker._flush_stream()
    val = out.getvalue()
    assert "ABC" not in val and "nota previa" in val


def test_eof_ignore_cancel_bounded_exit(env):
    agent = FakeAcpProc()
    agent._serve_prompt = lambda m: None
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    q = queue.Queue()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q,
        stdout=io.StringIO(), poll_state_seconds=0.05,
        today=lambda: "2026-01-01", verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex(
            "Local\\T-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    worker.cancel_grace = 0.5
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    for _ in range(200):
        if any(m["method"] == "session/prompt" for m in agent.received):
            break
        time.sleep(0.05)
    q.put(None)
    deadline = time.time() + 10
    while time.time() < deadline and not any(
            m["method"] == "session/cancel" for m in agent.received):
        time.sleep(0.05)
    assert any(m["method"] == "session/cancel" for m in agent.received)
    t.join(15)
    assert not t.is_alive()


def test_rollover_watcher_spawns_next_day_window(env, monkeypatch):
    clock = {"day": "2026-01-01"}
    group = "local"
    mname = "Local\\SDBRoll-" + os.urandom(6).hex()
    monkeypatch.setattr(bridge, "worker_mutex_name", lambda g: mname)
    source = bridge.OccurrenceSource(
        env.db, env.policy["_occurrences_sql"], env.policy["_highwater_sql"])
    store = bridge.StateStore(env.state_root / group)
    spawns = []
    watcher = bridge.Watcher(
        env.policy, group, source, store,
        spawn_worker=lambda p, g, d: spawns.append((g, d)) or FakeChild(),
        is_worker_alive=bridge.worker_alive,
        now=lambda: 1000.0, today=lambda: clock["day"])
    watcher.poll_once()
    add_occurrence(env.db, level="ERROR", message="ayer")
    watcher.poll_once()
    assert spawns == [(group, "2026-01-01")]
    agent = FakeAcpProc()
    q = queue.Queue()
    worker = bridge.ConsoleWorker(
        env.policy, group, "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q,
        stdout=io.StringIO(), poll_state_seconds=0.05,
        today=lambda: clock["day"], verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex(mname),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    for _ in range(200):
        if store.load()["completed"]:
            break
        time.sleep(0.05)
    assert store.load()["completed"]
    clock["day"] = "2026-01-02"
    add_occurrence(env.db, level="ERROR", message="hoy")
    watcher.poll_once()
    t.join(15)
    assert not t.is_alive()
    watcher.poll_once()
    watcher.poll_once()
    assert (group, "2026-01-02") in spawns
    st = store.load()
    assert st["paused"] is False


def _stderr_write(agent, text):
    os.write(agent._err_w, (text + "\n").encode("utf-8"))


def test_devin_stderr_info_never_on_console(env):
    agent = FakeAcpProc()
    _stderr_write(agent, "INFO run_acp_server skills discovery starting")
    _stderr_write(agent, "INFO run_acp_server skills discovery done")
    _stderr_write(agent, "DEBUG transport tick")
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    worker.run()
    val = out.getvalue()
    assert "skills discovery" not in val and "run_acp_server" not in val
    assert "transport tick" not in val
    diag = store.state_dir / "devin-diagnostics.log"
    assert diag.exists()
    assert "skills discovery" in diag.read_text(encoding="utf-8")


def test_devin_stderr_error_banner_once_and_detalle(env):
    agent = FakeAcpProc()
    _stderr_write(agent, "ERROR rust panic stream broke")
    _stderr_write(agent, "ERROR rust panic stream broke")
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store,
                                          stdin_lines=["/detalle"])
    worker.run()
    val = out.getvalue()
    assert val.count("problema técnico") == 1
    assert "panic" in val
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 1


def test_tool_status_spanish_dedup(env):
    def hook(msg, agent):
        sid = msg["params"]["sessionId"]
        for _ in range(2):
            agent._write({"jsonrpc": "2.0", "method": "session/update",
                          "params": {"sessionId": sid, "update": {
                              "sessionUpdate": "tool_call",
                              "toolCallId": "t1", "title": "leer archivo",
                              "status": "in_progress"}}})
        agent._write({"jsonrpc": "2.0", "method": "session/update",
                      "params": {"sessionId": sid, "update": {
                          "sessionUpdate": "tool_call_update",
                          "toolCallId": "t1", "title": "leer archivo",
                          "status": "completed"}}})
    agent = FakeAcpProc(prompt_hook=hook)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    worker.run()
    val = out.getvalue()
    assert val.count("Devin está revisando: leer archivo") == 1
    assert val.count("Acción terminada: leer archivo") == 1


def test_friendly_header_and_estado(env):
    agent = FakeAcpProc()
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store,
                                          stdin_lines=["/estado"])
    worker.run()
    val = out.getvalue()
    assert "Sofía + Devin" in val
    assert "Apps de esta PC" in val and "administrador" in val
    assert "esperando" in val.lower() or "procesando" in val.lower()


def test_ui_events_jsonl_contract(env):
    agent = FakeAcpProc()
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    worker.run()
    log = store.state_dir / "ui-events.jsonl"
    assert log.exists()
    events = [json.loads(l) for l in
              log.read_text(encoding="utf-8").splitlines() if l.strip()]
    kinds = [e["event"] for e in events]
    assert "console_ready" in kinds and "session_ready" in kinds
    started = [e for e in events if e["event"] == "prompt_started"]
    finished = [e for e in events if e["event"] == "prompt_completed"]
    assert started and started[0]["event_ids"] == [1]
    assert finished and finished[0]["event_ids"] == [1]
    assert finished[0]["stop_reason"] == "end_turn"
    sready = [e for e in events if e["event"] == "session_ready"][0]
    assert sready["session_id"] and sready["resumed"] is False
    msgs = [e for e in events if e["event"] == "agent_message"]
    assert msgs and all(isinstance(m["event_ids"], list) for m in msgs)
    assert msgs[0]["event_ids"] == [1]
    assert all("event_ids" in e for e in started + finished)


def test_permission_during_session_new(env):
    opts = [{"optionId": "allow", "name": "Permitir", "kind": "allow_once"},
            {"optionId": "reject", "name": "Rechazar", "kind": "reject_once"}]

    class PermOnNew(FakeAcpProc):
        def _handle(self, msg):
            if msg.get("method") == "session/new":
                def ask_then_reply():
                    self.ask_permission(opts)
                    self._write({"jsonrpc": "2.0", "id": msg["id"],
                                 "result": self._session_result("sess-perm")})
                threading.Thread(target=ask_then_reply, daemon=True).start()
                return
            super()._handle(msg)

    agent = PermOnNew()
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent,
                                          stdin_lines=["1"])
    assert worker.run() == 0
    val = out.getvalue()
    assert "autorización" in val or "permiso" in val.lower()
    assert any(m["method"] == "session/prompt" for m in agent.received)


def test_queue_change_notification_only_on_change(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    hold = threading.Event()
    agent = FakeAcpProc(hold_prompt=hold)
    q = queue.Queue()
    out = io.StringIO()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q, stdout=out,
        poll_state_seconds=0.05, today=lambda: "2026-01-01",
        verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex(
            "Local\\T-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    for _ in range(200):
        if any(m["method"] == "session/prompt" for m in agent.received):
            break
        time.sleep(0.05)
    time.sleep(0.8)
    with store.transaction() as st:
        st["pending"].append(ev(2))
    time.sleep(0.8)
    with store.transaction() as st:
        st["pending"].append(ev(3))
    time.sleep(0.8)
    hold.set()
    q.put(None)
    t.join(15)
    val = out.getvalue()
    assert val.count("Sofía agregó nuevos avisos") == 2


def test_ui_events_no_secret_and_turn_boundaries(env):
    agent = FakeAcpProc(stop_reason="cancelled")
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    worker.run()
    log = store.state_dir / "ui-events.jsonl"
    events = [json.loads(l) for l in
              log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert not [e for e in events if e["event"] == "prompt_completed"
                and e.get("stop_reason") == "end_turn"]
    assert any(e["event"] == "prompt_started" for e in events)


def test_start_dispatchers_only_once(env, monkeypatch):
    agent = FakeAcpProc()
    store = bridge.StateStore(env.state_root / "local")
    q = queue.Queue()
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store,
                                        stdin_lines=[])
    calls = []
    real_thread = threading.Thread

    class SpyThread(real_thread):
        def start(self):
            calls.append(getattr(self, "_target", None))
            super().start()

    monkeypatch.setattr(bridge.threading, "Thread", SpyThread)
    worker._start_dispatchers()
    worker._start_dispatchers()
    assert len(calls) == 1
    assert calls[0] == worker._stdin_dispatch_loop
    worker._stop.set()
    q.put(None)


def test_info_line_with_error_word_no_banner(env):
    agent = FakeAcpProc()
    _stderr_write(agent, "INFO catalogo contiene texto ERROR literal")
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    worker.run()
    assert "problema técnico" not in out.getvalue()


def test_detalle_shows_last_error_after_info_flood(env):
    agent = FakeAcpProc()
    _stderr_write(agent, "ERROR fallo critico al cargar skills")
    for i in range(60):
        _stderr_write(agent, f"INFO run_acp_server ruido {i}")
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store,
                                          stdin_lines=["/detalle"])
    worker.run()
    val = out.getvalue()
    assert val.count("problema técnico") == 1
    assert "fallo critico al cargar skills" in val
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 1


def test_completion_on_own_line_after_partial_chunk(env):
    agent = FakeAcpProc()
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    worker.run()
    assert "\nDevin terminó este lote" in out.getvalue()


def test_permission_journal_carries_active_event_ids(env):
    opts = [{"optionId": "allow", "name": "Permitir", "kind": "allow_once"},
            {"optionId": "reject", "name": "Rechazar", "kind": "reject_once"}]

    def hook(msg, agent):
        agent.ask_permission(opts)

    agent = FakeAcpProc(prompt_hook=hook)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent,
                                          stdin_lines=["1"], store=store)
    worker.run()
    log = store.state_dir / "ui-events.jsonl"
    events = [json.loads(l) for l in
              log.read_text(encoding="utf-8").splitlines() if l.strip()]
    requested = [e for e in events if e["event"] == "permission_requested"]
    resolved = [e for e in events if e["event"] == "permission_resolved"]
    assert any(e["event_ids"] == [1] for e in requested)
    assert any(e["event_ids"] == [1] for e in resolved)
    assert all(isinstance(e["event_ids"], list) for e in requested + resolved)


def test_resume_with_uncertain_shows_banner_no_send(env):
    agent = FakeAcpProc(load_session=True)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
        st["uncertain"] = True
        st["inflight"] = {"event_ids": [1], "prompt_sent": True,
                          "uncertain": True, "use_initial": True,
                          "day": "2026-01-01", "stop_reason": None}
        st["sessions"]["2026-01-01"] = {"session_id": "sess-prev",
                                        "initial_prompt_sent": True}
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    worker.run()
    val = out.getvalue()
    assert "Hay un turno anterior sin confirmación" in val
    methods = [m["method"] for m in agent.received]
    assert "session/load" in methods
    assert "session/new" not in methods
    assert "session/prompt" not in methods


def test_resumed_session_configured_bypass_before_prompt(env):
    agent = FakeAcpProc()
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
        st["sessions"]["2026-01-01"] = {"session_id": "sess-prev",
                                        "initial_prompt_sent": True}
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    worker.run()
    methods = [m["method"] for m in agent.received]
    assert "session/load" in methods and "session/new" not in methods
    cfg = [m for m in agent.received
           if m["method"] == "session/set_config_option"]
    assert cfg
    assert cfg[0]["params"] == {"sessionId": "sess-prev",
                              "configId": "mode", "value": "bypass"}
    assert methods.index("session/set_config_option") \
        < methods.index("session/prompt")
    assert agent.mode_value == "bypass"
    assert "Modo de sesión: /bypass" in out.getvalue()
    events = [json.loads(l) for l in
              (store.state_dir / "ui-events.jsonl")
              .read_text(encoding="utf-8").splitlines() if l.strip()]
    conf = [e for e in events if e["event"] == "permission_mode_configured"]
    assert conf and conf[0]["effective_mode"] == "bypass"


def test_new_session_configured_bypass(env):
    agent = FakeAcpProc()
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    worker.run()
    cfg = [m for m in agent.received
           if m["method"] == "session/set_config_option"]
    assert cfg and cfg[0]["params"]["value"] == "bypass"
    assert cfg[0]["params"]["sessionId"].startswith("sess-")


def test_mode_rpc_rejected_fails_closed(env):
    agent = FakeAcpProc(mode_reject=True)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    assert worker.run() == 5
    methods = [m["method"] for m in agent.received]
    assert "session/set_config_option" in methods
    assert "session/prompt" not in methods
    st = store.load()
    assert [e["occurrence_id"] for e in st["pending"]] == [1]
    assert st["inflight"] is None and st["uncertain"] is False
    assert st["status"] == "permission_mode_error"


def test_mode_ack_mismatch_fails_closed(env):
    agent = FakeAcpProc(mode_ack_mismatch=True)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    assert worker.run() == 5
    assert not [m for m in agent.received
                if m["method"] == "session/prompt"]
    assert store.load()["status"] == "permission_mode_error"


def test_mode_option_missing_fails_closed(env):
    agent = FakeAcpProc(mode_option_missing=True)
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    worker, wstore, out, ph = make_worker(env, agent=agent, store=store)
    assert worker.run() == 5
    assert not [m for m in agent.received
                if m["method"] == "session/prompt"]


def test_unknown_policy_mode_fails_closed(env):
    import copy
    policy = copy.deepcopy(env.policy)
    policy["permission_mode"] = "nonsense-mode"
    agent = FakeAcpProc()
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1)]
    q = queue.Queue()
    q.put(None)
    out = io.StringIO()
    worker = bridge.ConsoleWorker(
        policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q, stdout=out,
        poll_state_seconds=0.05, today=lambda: "2026-01-01",
        verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex(
            "Local\\T-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    assert worker.run() == 5
    assert not [m for m in agent.received
                if m["method"] == "session/prompt"]
    assert [e["occurrence_id"] for e in store.load()["pending"]] == [1]


def test_spawn_agent_argv_includes_mode(monkeypatch):
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["env"] = kw.get("env")
        captured["cwd"] = kw.get("cwd")
        return Simple()

    monkeypatch.setattr(bridge.subprocess, "Popen", fake_popen)
    bridge.default_spawn_agent("devin.exe", "dangerous", "C:/tmp")
    assert captured["argv"] == ["devin.exe", "--permission-mode",
                                "dangerous", "acp"]
    assert captured["env"]["DEVIN_PERMISSION_MODE"] == "dangerous"
    assert captured["cwd"] == "C:/tmp"


def test_healthy_worker_hard_close_pauses(env):
    now = {"t": 7000.0}
    w, store, spawns = make_watcher(
        env, is_worker_alive=lambda g: False, now=lambda: now["t"])
    w.poll_once()
    with store.transaction() as st:
        st["pending"] = [ev(1)]
        st["worker_ready"] = True
        st["spawn_counted"] = False
        st["last_spawn"] = now["t"] - 1
    w.poll_once()
    st = store.load()
    assert st["paused"] is True
    assert spawns == []


def test_transport_idle_timeout_raises_stall():
    proc = FakeAcpProc(hold_prompt=threading.Event())
    tr = bridge.AcpTransport(proc, on_notification=lambda m: None,
                             on_request=lambda m: {})
    try:
        with pytest.raises(bridge.AcpStallError):
            tr.request("session/prompt", {"sessionId": "s", "prompt": []},
                       timeout=5, idle_timeout=0.3)
    finally:
        tr.close()


def test_transport_request_without_idle_timeout_still_works():
    proc = FakeAcpProc()
    tr = bridge.AcpTransport(proc, on_notification=lambda m: None,
                             on_request=lambda m: {})
    try:
        res = tr.request("initialize", {"protocolVersion": 1})
        assert res["protocolVersion"] == 1
    finally:
        tr.close()


def test_health_snapshot_ok_and_stale(env):
    w, store, spawns = make_watcher(env)
    payload, status = w.health_snapshot()
    assert status == 503 and payload["ok"] is False  # todavia no polleo
    w._last_poll_at = w.now()
    payload, status = w.health_snapshot()
    assert status == 200 and payload["ok"] is True
    assert payload["group"] == "local"
    w._last_poll_at = w.now() - 10_000
    assert w.health_snapshot()[1] == 503


def test_health_snapshot_paused_is_503(env):
    w, store, spawns = make_watcher(env)
    w._last_poll_at = w.now()
    with store.transaction() as st:
        st["paused"] = True
        st["status"] = "paused"
    payload, status = w.health_snapshot()
    assert status == 503 and payload["ok"] is False
    assert payload["status"] == "paused"


def test_pause_self_heals_when_report_arrived_after_pause(env):
    w, store, spawns = make_watcher(env)
    w.poll_once()
    add_occurrence(env.db, level="ERROR")  # id 1
    with store.transaction() as st:
        st["initialized"] = True
        st["cursor"] = 1
        st["paused"] = True
        st["status"] = "paused"
        st["pause_cursor"] = 1
        st["pending"] = [ev(2)]  # encolado en un ciclo previo a la pausa
    w.poll_once()  # no hay filas nuevas: la curacion no depende de queued
    st = store.load()
    assert st["paused"] is False and st["status"] == "ok"
    assert len(spawns) == 1


def test_pause_stays_without_report_after_pause(env):
    w, store, spawns = make_watcher(env)
    w.poll_once()
    with store.transaction() as st:
        st["initialized"] = True
        st["cursor"] = 5
        st["paused"] = True
        st["status"] = "paused"
        st["pause_cursor"] = 5
        st["pending"] = [ev(4)]
    w.poll_once()
    st = store.load()
    assert st["paused"] is True and spawns == []


@pytest.mark.skipif(os.name != "nt", reason="psapi solo Windows")
def test_process_commit_bytes_current_process(env):
    commit = bridge.process_commit_bytes(os.getpid())
    assert commit is not None and commit > 0


def test_worker_recycles_agent_over_commit_limit(env):
    procs = []

    def factory():
        p = FakeAcpProc()
        procs.append(p)
        return p

    worker, wstore, out, ph = make_worker(
        env, agent_factory=factory,
        commit_fn=lambda pid: 8 * 1024 ** 3)
    assert worker._connect()
    assert worker._should_recycle_agent() is True
    assert worker._recycle_agent() is True
    assert len(procs) == 2
    assert worker._session_id is not None


def test_worker_keeps_agent_under_commit_limit(env):
    worker, wstore, out, ph = make_worker(
        env, commit_fn=lambda pid: 1024 ** 2)
    assert worker._connect()
    assert worker._should_recycle_agent() is False


def test_health_server_serves_json(env):
    import urllib.request
    w, store, spawns = make_watcher(env)
    w._last_poll_at = w.now()
    httpd = bridge.start_health_server(0, w.health_snapshot)
    assert httpd is not None
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is True and body["group"] == "local"
    finally:
        httpd.shutdown()


def test_worker_recovers_from_stalled_turn(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1, message="colgado")]
    procs = []

    def factory():
        p = FakeAcpProc()
        if not procs:
            p.hold_prompt = threading.Event()
        procs.append(p)
        return p

    worker, wstore, out, ph = make_worker(env, agent_factory=factory)
    worker.policy["stall_seconds"] = 0.3
    assert worker.run() == 0
    assert len(procs) >= 2
    st = wstore.load()
    assert st["completed"] == [1] and st["pending"] == []
    prompts = [m for m in procs[-1].received
               if m["method"] == "session/prompt"]
    assert len(prompts) == 1
    assert "colgado" in prompts[0]["params"]["prompt"][0]["text"]


def test_worker_retries_batch_on_max_tokens(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1, message="cortado")]
    calls = {"n": 0}

    def hook(msg, proc):
        calls["n"] += 1
        proc.stop_reason = "max_tokens" if calls["n"] == 1 else "end_turn"

    agent = FakeAcpProc(prompt_hook=hook)
    worker, wstore, out, ph = make_worker(env, agent=agent)
    assert worker.run() == 0
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 2
    st = wstore.load()
    assert st["completed"] == [1] and st["pending"] == []
    assert st["uncertain"] is False and st["inflight"] is None


def test_worker_heals_truncated_turn_on_start(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["pending"] = [ev(1, message="cortado")]
        st["inflight"] = {"event_ids": [1], "prompt_sent": True,
                          "uncertain": True, "use_initial": False,
                          "day": "2026-01-01", "stop_reason": "max_tokens"}
        st["uncertain"] = True
        st["status"] = "turn_incomplete:max_tokens"
    agent = FakeAcpProc()
    worker, wstore, out, ph = make_worker(env, agent=agent)
    assert worker.run() == 0
    st = wstore.load()
    assert st["completed"] == [1] and st["pending"] == []
    assert st["uncertain"] is False and st["inflight"] is None


def test_render_digest_groups_orders_and_redacts(env):
    crit = ev(10, message="backend caido", level="CRITICAL")
    err = ev(11, message="column branch_id no existe", level="ERROR")
    dup = ev(12, message="column branch_id no existe", level="ERROR")
    dup["issue_id"] = err["issue_id"]
    warn = ev(13, message="warning ruidoso", level="WARNING")
    text = bridge.render_digest([warn, dup, err, crit], "2026-01-01")
    assert "RESUMEN DE COLA: 4 avisos" in text
    assert text.index("[CRITICAL]") < text.index("[ERROR]") \
        < text.index("[WARNING]")
    assert "x2" in text
    assert "occurrences: 11, 12" in text
    assert "occurrences: 10" in text
    assert "sofia.db" in text


def test_render_digest_caps_size(env):
    events = [ev(i, message="mensaje " + chr(97 + i % 26) + "x" * 200)
              for i in range(1, 41)]
    text = bridge.render_digest(events, "2026-01-01", max_chars=2000)
    assert len(text) <= 2600
    assert "omitidos por tamano" in text


def test_worker_sends_single_digest_for_backlog(env):
    store = bridge.StateStore(env.state_root / "local")
    events = [ev(i, message="aviso %d" % i) for i in range(1, 61)]
    with store.transaction() as st:
        st["initialized"] = True
        st["cursor"] = 60
        st["pending"] = events
    agent = FakeAcpProc()
    worker, wstore, out, _ = make_worker(env, agent=agent)
    assert worker.run() == 0
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 1
    text = prompts[0]["params"]["prompt"][0]["text"]
    assert "RESUMEN DE COLA: 60 avisos" in text
    st = wstore.load()
    assert st["pending"] == []
    assert st["completed"] == list(range(1, 61))


def test_worker_keeps_normal_batches_below_threshold(env):
    store = bridge.StateStore(env.state_root / "local")
    events = [ev(i, message="aviso %d" % i) for i in range(1, 51)]
    with store.transaction() as st:
        st["initialized"] = True
        st["cursor"] = 50
        st["pending"] = events
    agent = FakeAcpProc()
    worker, wstore, out, _ = make_worker(env, agent=agent)
    assert worker.run() == 0
    prompts = [m for m in agent.received if m["method"] == "session/prompt"]
    assert len(prompts) == 1
    assert "RESUMEN DE COLA" not in prompts[0]["params"]["prompt"][0]["text"]
    assert wstore.load()["pending"] == []


def test_worker_closes_after_idle_timeout(env):
    env.policy["idle_close_minutes"] = 0.001
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["worker_ready"] = True
        st["last_spawn"] = time.time()
        st["spawn_counted"] = True
        st["sessions"]["2026-01-01"] = {"session_id": "sess-1",
                                        "initial_prompt_sent": True}
    agent = FakeAcpProc()
    q = queue.Queue()
    out = io.StringIO()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: agent, stdin_queue=q, stdout=out,
        poll_state_seconds=0.02, today=lambda: "2026-01-01",
        verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex(
            "Local\\T6-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    assert worker.run() == 0
    st = store.load()
    assert st["worker_ready"] is False
    assert st["last_spawn"] == 0.0
    assert st["paused"] is False
    assert "cierro esta consola" in out.getvalue()


def test_worker_exits_clean_when_agent_dies_between_turns(env):
    store = bridge.StateStore(env.state_root / "local")
    with store.transaction() as st:
        st["initialized"] = True
        st["worker_ready"] = True
        st["last_spawn"] = time.time() - 30
        st["spawn_counted"] = True
    q = queue.Queue()
    out = io.StringIO()
    worker = bridge.ConsoleWorker(
        env.policy, "local", "2026-01-01", store,
        spawn_agent=lambda e, m, c: FakeAcpProc(), stdin_queue=q,
        stdout=out, poll_state_seconds=0.02, today=lambda: "2026-01-01",
        verify_elevation_fn=lambda c: None,
        mutex_factory=lambda n: bridge.HeldMutex(
            "Local\\T7-" + os.urandom(6).hex()),
        auth_fn=lambda e: True, title_fn=lambda t: None)
    worker._transport = Simple(closed=True)
    assert worker._main_loop() == 0
    st = store.load()
    assert st["worker_ready"] is False
    assert st["last_spawn"] == 0.0
    assert st["next_retry_after"] > time.time()
    assert st["paused"] is False
    assert "cierro esta consola" in out.getvalue()


def test_default_spawn_worker_starts_minimized_console(monkeypatch, tmp_path):
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(bridge.subprocess, "Popen", FakePopen)
    policy = {"python_executable": "python.exe", "_dir": str(tmp_path)}
    bridge.default_spawn_worker(policy, "local", "2026-01-01")
    kwargs = captured["kwargs"]
    assert kwargs["creationflags"] == bridge.CREATE_NEW_CONSOLE
    assert captured["cmd"][:3] == ["python.exe", str(bridge.SCRIPT_PATH),
                                   "worker"]
    if os.name == "nt":
        si = kwargs["startupinfo"]
        assert si is not None
        assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert si.wShowWindow == bridge.SW_SHOWMINNOACTIVE
    else:
        assert kwargs["startupinfo"] is None
