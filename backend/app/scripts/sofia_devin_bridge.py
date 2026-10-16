"""Sofia desktop Devin bridge.

Watcher oculto read-only de occurrences -> cola durable por grupo -> una consola
visible por grupo/dia que habla con el Devin CLI oficial via `devin acp`
(JSON-RPC por stdio). Consola custom sobre ACP real; no TUI nativo ni
inyeccion de foco/clipboard.

CLI:
    python sofia_devin_bridge.py watch  --group local [--once]
    python sofia_devin_bridge.py worker --group local --day YYYY-MM-DD
"""

from __future__ import annotations

import argparse
from collections import deque
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import logging
import logging.handlers
import os
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback as tb_mod
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
DEFAULT_POLICY_DIR = REPO_ROOT / ".devin" / "sofia-cli"

STATE_VERSION = 1
MAX_PENDING = 500
CONTROL_RPC_TIMEOUT = 60
PROMPT_RESPONSE_WAIT = 24 * 3600
STATE_POLL_SECONDS = 5.0
MAX_SPAWN_FAILURES = 5
SPAWN_GRACE_SECONDS = 15.0
MAX_COMPLETED = 1000
STALL_SECONDS = 900.0
MAX_STALL_RETRIES = 1
STALL_WAIT_SLICE = 0.25
HEALTH_STALE_SECONDS = 120.0
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
WORKER_FAIL_STATUS = {
    "agent_missing", "agent_spawn_error", "agent_init_error", "session_error",
    "elevation_mismatch", "auth_required", "manual_gap", "spawn_error",
    "permission_mode_error",
}


class BridgeError(Exception):
    pass


class StateError(BridgeError):
    pass


class ElevationMismatch(BridgeError):
    pass


class AcpError(BridgeError):
    pass


class AcpRemoteError(AcpError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"ACP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class AcpStallError(AcpError):
    """El agente acepto la peticion pero dejo de emitir actividad."""
    pass


_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL)
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]+")
_CRED_RE = re.compile(
    r"(?i)(\b(?:authorization|password|passwd|secret|api[_-]?key|authtoken|"
    r"access[_-]?token|client[_-]?secret)\b[\"']?\s*[:=]\s*[\"']?)[^\s,}\"']+")
_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>?]+)\?[^\s\"'<>]+")
_URL_USERINFO_RE = re.compile(r"(?i)\b((?:https?|rtsp|rtmp|ftp)://)[^/\s:@]+:[^@\s]+@")
_TOKEN_PATH_RE = re.compile(
    r"(?i)(/(?:api|hooks?|token|auth|webhook|events)/)[A-Za-z0-9._\-]{8,}(?=[/?\s\"']|$)")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_JSON_RE = re.compile(
    r"(?i)(\"(?:phone|whatsapp_number|recipient_number|telefono)\"\s*:\s*\")[^\"]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_LONGNUM_RE = re.compile(r"\b\d{8,15}\b")
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-B]|[@-Z\\-_])")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def redact_text(text: Any) -> str:
    text = str(text)
    text = _PEM_RE.sub("[CLAVE_PRIVADA_REDACTADA]", text)
    text = _ANSI_RE.sub("", text)
    text = _BEARER_RE.sub(r"\1 [REDACTADO]", text)
    text = _CRED_RE.sub(r"\1[REDACTADO]", text)
    text = _URL_QUERY_RE.sub(r"\1?[REDACTADO]", text)
    text = _URL_USERINFO_RE.sub(r"\1[REDACTADO]@", text)
    text = _TOKEN_PATH_RE.sub(r"\1[REDACTADO]", text)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _PHONE_JSON_RE.sub(r"\1[REDACTADO]", text)
    text = _JWT_RE.sub("[TOKEN_REDACTADO]", text)
    text = _LONGNUM_RE.sub("[NUMERO_REDACTADO]", text)
    text = _CTRL_RE.sub("", text)
    return text


REQUIRED_POLICY_KEYS = ("source_db", "state_root", "devin_executable", "groups")


def load_policy(policy_dir: Path) -> Dict[str, Any]:
    policy_dir = Path(policy_dir)
    try:
        policy = json.loads((policy_dir / "policy.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"policy.json invalido: {exc}") from exc
    for key in REQUIRED_POLICY_KEYS:
        if key not in policy:
            raise BridgeError(f"policy.json sin clave requerida: {key}")
    policy["_dir"] = policy_dir
    for name, group in policy["groups"].items():
        for key in ("title", "cwd", "elevated", "initial_prompt", "task_name"):
            if key not in group:
                raise BridgeError(f"policy grupo {name} sin clave {key}")
        try:
            group["_initial_template"] = (
                policy_dir / group["initial_prompt"]).read_text(encoding="utf-8")
        except OSError as exc:
            raise BridgeError(f"prompt inicial ilegible: {exc}") from exc
    for sql_key, fname in (("_occurrences_sql", "occurrences.sql"),
                           ("_highwater_sql", "highwater.sql")):
        try:
            policy[sql_key] = (policy_dir / fname).read_text(encoding="utf-8")
        except OSError as exc:
            raise BridgeError(f"SQL ilegible {fname}: {exc}") from exc
    route = policy.get("elenatec_route", {})
    policy["_elenatec"] = {
        "sources": {str(s).lower() for s in route.get("sources", [])},
        "service_ids": {str(s).lower() for s in route.get("service_ids", [])},
        "service_prefixes": tuple(
            str(s).lower() for s in route.get("service_prefixes", [])),
    }
    policy["_levels"] = {str(l).upper() for l in policy.get("levels", [])}
    return policy


def route_event(policy: Dict[str, Any], row: Dict[str, Any]) -> Optional[str]:
    level = str(row.get("level") or "").upper()
    if level not in policy["_levels"]:
        return None
    route = policy["_elenatec"]
    source = str(row.get("source") or "").lower()
    service_id = str(row.get("service_id") or "").lower()
    if source in route["sources"] or service_id in route["service_ids"]:
        return "elenatec"
    if service_id and service_id.startswith(route["service_prefixes"]):
        return "elenatec"
    return "local"


EXPECTED_OCCURRENCE_COLUMNS = {
    "occurrence_id", "issue_id", "occurrence_timestamp", "service_id",
    "service_name", "level", "message", "source", "issue_environment",
    "issue_release", "detail", "traceback", "breadcrumbs",
}


class OccurrenceSource:
    def __init__(self, db_path: Path, occurrences_sql: str, highwater_sql: str):
        self.db_path = Path(db_path)
        self._occ_sql = occurrences_sql
        self._hw_sql = highwater_sql
        self._validated = False

    def _connect(self) -> sqlite3.Connection:
        uri = self.db_path.absolute().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.execute("PRAGMA query_only=ON")
        return conn

    def highwater(self) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = conn.execute(self._hw_sql).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()

    def fetch(self, after_id: int, batch_size: int) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                self._occ_sql,
                {"after_id": int(after_id), "batch_size": int(batch_size)})
            cols = {d[0] for d in cur.description or []}
            if not self._validated:
                missing = EXPECTED_OCCURRENCE_COLUMNS - cols
                if missing:
                    raise BridgeError(
                        f"esquema occurrences inesperado, faltan: {sorted(missing)}")
                self._validated = True
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]
        finally:
            conn.close()


def _initial_state() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION, "initialized": False, "cursor": 0,
        "sessions": {}, "pending": [], "inflight": None, "completed": [],
        "paused": False, "source_reset": False, "uncertain": False,
        "status": "ok", "status_at": 0.0, "last_error": None,
        "spawn_failures": 0, "next_retry_after": 0.0, "last_spawn": 0.0,
        "spawn_counted": False, "worker_ready": False, "circuit_open": False,
    }


def _valid_event(ev: Any) -> bool:
    return (
        isinstance(ev, dict)
        and isinstance(ev.get("occurrence_id"), int)
        and isinstance(ev.get("received_day"), str)
        and isinstance(ev.get("level"), str)
        and isinstance(ev.get("message"), str)
    )


def validate_state(state: Any, existing: bool = True) -> Dict[str, Any]:
    if not isinstance(state, dict):
        raise StateError("estado no es un objeto")
    base = _initial_state()
    if existing:
        missing = set(base) - set(state)
        if missing:
            raise StateError(f"estado sin claves: {sorted(missing)}")
    for key, default in base.items():
        state.setdefault(key, default)
    if state.get("version") != STATE_VERSION:
        raise StateError(f"version de estado no soportada: {state.get('version')}")
    checks = (
        ("initialized", bool), ("cursor", int), ("sessions", dict),
        ("pending", list), ("completed", list), ("paused", bool),
        ("source_reset", bool), ("uncertain", bool), ("status", str),
        ("status_at", (int, float)), ("spawn_failures", int),
        ("next_retry_after", (int, float)), ("last_spawn", (int, float)),
        ("spawn_counted", bool), ("worker_ready", bool), ("circuit_open", bool),
    )
    for key, typ in checks:
        if not isinstance(state[key], typ):
            raise StateError(f"campo de estado invalido: {key}")
    if state["cursor"] < 0:
        raise StateError("cursor negativo")
    if state["completed"] and not all(
            isinstance(i, int) and i >= 0 for i in state["completed"]):
        raise StateError("completed invalido")
    for day, sess in state["sessions"].items():
        if not isinstance(day, str) or not isinstance(sess, dict) \
                or not isinstance(sess.get("session_id"), str) \
                or not isinstance(sess.get("initial_prompt_sent"), bool):
            raise StateError("sessions invalido")
    if not all(_valid_event(e) for e in state["pending"]):
        raise StateError("pending invalido")
    inflight = state["inflight"]
    if inflight is not None:
        if not isinstance(inflight, dict) \
                or not all(isinstance(i, int) for i in inflight.get("event_ids", [])) \
                or not isinstance(inflight.get("prompt_sent"), bool) \
                or not isinstance(inflight.get("uncertain"), bool) \
                or not isinstance(inflight.get("use_initial"), bool) \
                or not isinstance(inflight.get("day"), str):
            raise StateError("inflight invalido")
    return state


def _mutex_suffix(state_dir: Path) -> str:
    return hashlib.sha1(
        str(state_dir.resolve()).lower().encode("utf-8")).hexdigest()[:12]


class _Kernel32:
    _lib = None

    @classmethod
    def lib(cls):
        if cls._lib is None:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            k.CreateMutexW.restype = wintypes.HANDLE
            k.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            k.OpenMutexW.restype = wintypes.HANDLE
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            k.CloseHandle.restype = wintypes.BOOL
            k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k.WaitForSingleObject.restype = wintypes.DWORD
            k.ReleaseMutex.argtypes = [wintypes.HANDLE]
            k.ReleaseMutex.restype = wintypes.BOOL
            k.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
            k.SetConsoleTitleW.restype = wintypes.BOOL
            cls._lib = k
        return cls._lib


class HeldMutex:
    """Mutex de existencia: el poseedor lo retiene mientras vive."""

    def __init__(self, name: str):
        self.name = name
        self._handle = None
        self._flag: Optional[Path] = None
        self._token = os.urandom(8).hex()

    def _flag_path(self) -> Path:
        return Path(tempfile.gettempdir()) / (
            "sdb-" + hashlib.sha1(self.name.encode()).hexdigest()[:16] + ".lock")

    def acquire(self) -> bool:
        if os.name == "nt":
            k = _Kernel32.lib()
            handle = k.CreateMutexW(None, True, self.name)
            if not handle:
                return False
            if ctypes.get_last_error() == 183:
                k.CloseHandle(handle)
                return False
            self._handle = handle
            return True
        flag = self._flag_path()
        try:
            fd = os.open(str(flag), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
        os.write(fd, self._token.encode())
        os.close(fd)
        self._flag = flag
        return True

    def release(self) -> None:
        if self._handle is not None:
            _Kernel32.lib().CloseHandle(self._handle)
            self._handle = None
        if self._flag is not None:
            try:
                if self._flag.read_text() == self._token:
                    self._flag.unlink()
            except OSError:
                pass
            self._flag = None


def exists_mutex(name: str) -> bool:
    if os.name == "nt":
        k = _Kernel32.lib()
        handle = k.OpenMutexW(0x00100000, False, name)
        if handle:
            k.CloseHandle(handle)
            return True
        return False
    flag = Path(tempfile.gettempdir()) / (
        "sdb-" + hashlib.sha1(name.encode()).hexdigest()[:16] + ".lock")
    return flag.exists()


class LockMutex:
    """Mutex de exclusion con wait/release entre procesos."""

    WAIT_TIMEOUT = 0x00000102

    def __init__(self, name: str):
        self.name = name
        self._handle = None
        self._owned = False
        self._flag: Optional[Path] = None
        self._token = os.urandom(8).hex()

    def acquire(self, timeout_ms: int = 30000) -> bool:
        if os.name == "nt":
            k = _Kernel32.lib()
            if self._handle is None:
                self._handle = k.CreateMutexW(None, False, self.name)
                if not self._handle:
                    return False
            rc = k.WaitForSingleObject(self._handle, timeout_ms)
            if rc in (0, 0x80):
                self._owned = True
                return True
            return False
        flag = Path(tempfile.gettempdir()) / (
            "sdb-lock-" + hashlib.sha1(self.name.encode()).hexdigest()[:16] + ".lock")
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                fd = os.open(str(flag), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, self._token.encode())
                os.close(fd)
                self._flag = flag
                self._owned = True
                return True
            except OSError:
                time.sleep(0.05)
        return False

    def release(self) -> None:
        if os.name == "nt":
            if self._handle is not None and self._owned:
                _Kernel32.lib().ReleaseMutex(self._handle)
            self._owned = False
            return
        if self._owned and self._flag is not None:
            try:
                if self._flag.read_text() == self._token:
                    self._flag.unlink()
            except OSError:
                pass
        self._owned = False
        self._flag = None

    def close(self) -> None:
        if self._handle is not None and os.name == "nt":
            _Kernel32.lib().CloseHandle(self._handle)
            self._handle = None


def worker_mutex_name(group: str) -> str:
    return f"Local\\SofiaDevinBridgeWorker-{group}"


def watcher_mutex_name(group: str) -> str:
    return f"Local\\SofiaDevinBridgeWatcher-{group}"


def worker_alive(group: str) -> bool:
    return exists_mutex(worker_mutex_name(group))


def is_user_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def verify_elevation(group_cfg: Dict[str, Any]) -> None:
    expected = bool(group_cfg.get("elevated"))
    if is_user_admin() != expected:
        raise ElevationMismatch(
            f"elevacion esperada={expected}; ejecucion cancelada")


def devin_authenticated(executable: str) -> bool:
    try:
        proc = subprocess.run(
            [executable, "auth", "status"],
            capture_output=True, timeout=20)
        return proc.returncode == 0
    except Exception:
        return False


def set_console_title(title: str) -> None:
    if os.name == "nt":
        try:
            _Kernel32.lib().SetConsoleTitleW(title)
        except Exception:
            pass


class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "state.json"
        self._lock = threading.Lock()

    def _lock_name(self) -> str:
        return f"Local\\SofiaDevinState-{_mutex_suffix(self.state_dir)}"

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return _initial_state()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"estado corrupto en {self.path}: {exc}") from exc
        return validate_state(raw, existing=True)

    def _save(self, state: Dict[str, Any]) -> None:
        validate_state(state)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.state_dir), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @contextmanager
    def transaction(self):
        with self._lock:
            mutex = LockMutex(self._lock_name())
            if not mutex.acquire():
                mutex.close()
                raise StateError("no se pudo adquirir el lock de estado")
            try:
                state = self.load()
                yield state
                self._save(state)
            finally:
                mutex.release()
                mutex.close()


def event_from_row(row: Dict[str, Any], received_day: str) -> Dict[str, Any]:
    detail_parts = []
    for key in ("detail", "traceback", "breadcrumbs"):
        val = row.get(key)
        if val:
            detail_parts.append(f"[{key}] {redact_text(val)}")
    return {
        "occurrence_id": int(row["occurrence_id"]),
        "issue_id": int(row["issue_id"]) if row.get("issue_id") is not None else None,
        "timestamp": redact_text(row.get("occurrence_timestamp") or ""),
        "received_day": received_day,
        "service_id": redact_text(row.get("service_id") or ""),
        "service_name": redact_text(row.get("service_name") or ""),
        "level": str(row.get("level") or "").upper(),
        "source": redact_text(row.get("source") or ""),
        "message": redact_text(row.get("message") or ""),
        "environment": redact_text(row.get("issue_environment") or ""),
        "release": redact_text(row.get("issue_release") or ""),
        "detail": "\n".join(detail_parts),
    }


def render_report(events: List[Dict[str, Any]], day: str) -> str:
    lines = [f"Reportes del dia {day} ({len(events)}):", ""]
    for ev in events:
        lines.append(
            f"- [{ev['level']}] {ev['service_name'] or ev['service_id']} "
            f"(occurrence {ev['occurrence_id']}, issue {ev.get('issue_id')}) "
            f"a las {ev['timestamp']} | fuente: {ev['source'] or 'desconocida'}")
        lines.append(f"  mensaje: {ev['message']}")
        lines.append(
            f"  issue actual: environment={ev['environment'] or 'n/d'} "
            f"release={ev['release'] or 'n/d'}")
        if ev["detail"]:
            lines.append("  detalle:")
            for dline in ev["detail"].splitlines():
                lines.append(f"    {dline}")
        lines.append("")
    return "\n".join(lines)


def build_initial_prompt(template: str, day: str,
                         events: List[Dict[str, Any]]) -> str:
    return template.replace("{day}", day).replace(
        "{report}", render_report(events, day))


def default_spawn_worker(policy: Dict[str, Any], group: str,
                         day: str) -> subprocess.Popen:
    py = policy.get("python_executable") or sys.executable
    cmd = [py, str(SCRIPT_PATH), "worker", "--group", group, "--day", day,
           "--policy-dir", str(policy["_dir"])]
    return subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE)


def default_spawn_agent(executable: str, mode: str,
                        cwd: str) -> subprocess.Popen:
    env = dict(os.environ)
    argv = [executable]
    if mode:
        env["DEVIN_PERMISSION_MODE"] = mode
        argv += ["--permission-mode", mode]
    argv.append("acp")
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0, cwd=cwd or None, env=env)


class AcpTransport:
    def __init__(self, process, on_notification, on_request, stderr_sink=None):
        self._proc = process
        self._on_notification = on_notification
        self._on_request = on_request
        self._stderr_sink = stderr_sink or (lambda line: None)
        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._last_activity = time.monotonic()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._err_reader = threading.Thread(target=self._stderr_loop, daemon=True)
        self._reader.start()
        self._err_reader.start()

    def _stderr_loop(self) -> None:
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    self._stderr_sink(redact_text(line)[:500])
        except Exception:
            pass

    def _read_loop(self) -> None:
        try:
            for raw in iter(self._proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._last_activity = time.monotonic()
                self._dispatch(msg)
        finally:
            self._closed.set()
            with self._pending_lock:
                pendings = list(self._pending.values())
                self._pending.clear()
            for slot in pendings:
                slot["error"] = AcpError("transporte cerrado (EOF)")
                slot["event"].set()

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg) \
                and "method" not in msg:
            with self._pending_lock:
                slot = self._pending.pop(msg["id"], None)
            if slot is None:
                return
            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                slot["error"] = AcpRemoteError(
                    int(err.get("code", 0)), str(err.get("message", "")),
                    err.get("data"))
            else:
                slot["result"] = msg.get("result")
            slot["event"].set()
        elif "method" in msg and "id" in msg:
            self._handle_incoming_request(msg)
        elif "method" in msg:
            try:
                self._on_notification(msg)
            except Exception:
                pass

    def _handle_incoming_request(self, msg: Dict[str, Any]) -> None:
        try:
            result = self._on_request(msg)
            response = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32603, "message": str(exc)[:300]}}
        try:
            self._send(response)
        except Exception:
            pass

    def _send(self, msg: Dict[str, Any]) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        if self._closed.is_set():
            return
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except Exception:
            pass

    def seconds_since_activity(self) -> float:
        return time.monotonic() - self._last_activity

    def request(self, method: str, params: Dict[str, Any],
                timeout: float = CONTROL_RPC_TIMEOUT,
                idle_timeout: Optional[float] = None) -> Any:
        if self._closed.is_set():
            raise AcpError("transporte cerrado")
        with self._pending_lock:
            req_id = self._next_id
            self._next_id += 1
            slot = {"event": threading.Event(), "result": None, "error": None}
            self._pending[req_id] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method,
                        "params": params})
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise AcpError(f"no se pudo enviar {method}: {exc}") from exc
        start = time.monotonic()
        while not slot["event"].wait(STALL_WAIT_SLICE):
            now = time.monotonic()
            if now - start >= timeout:
                with self._pending_lock:
                    self._pending.pop(req_id, None)
                raise AcpError(f"timeout esperando respuesta de {method}")
            if idle_timeout is not None \
                    and now - self._last_activity >= idle_timeout:
                with self._pending_lock:
                    self._pending.pop(req_id, None)
                raise AcpStallError(
                    f"{method} sin actividad por {idle_timeout:.0f}s")
        if slot["error"] is not None:
            raise slot["error"]
        return slot["result"]

    def close(self) -> None:
        self._closed.set()
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
        except Exception:
            pass


_MODE_VALUE_MAP = {
    "dangerous": "bypass",
    "bypass": "bypass",
    "yolo": "bypass",
    "smart": "smart",
    "accept-edits": "accept-edits",
}


class AcpSession:
    def __init__(self, transport: AcpTransport):
        self.transport = transport
        self.capabilities: Dict[str, Any] = {}
        self.session_info: Dict[str, Dict[str, Any]] = {}

    def initialize(self) -> Dict[str, Any]:
        result = self.transport.request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "sofia-devin-bridge", "version": "1.0"},
            "clientCapabilities": {}})
        result = result or {}
        if result.get("protocolVersion") != 1:
            raise AcpError(
                f"protocolVersion no soportada: {result.get('protocolVersion')}")
        self.capabilities = result.get("agentCapabilities", result) or {}
        return result

    def load_supported(self) -> bool:
        return bool(self.capabilities.get("loadSession"))

    def new_session(self, cwd: str) -> str:
        result = self.transport.request(
            "session/new", {"cwd": cwd, "mcpServers": []})
        sid = (result or {}).get("sessionId")
        if not sid:
            raise AcpError("session/new sin sessionId")
        self.session_info[str(sid)] = result or {}
        return str(sid)

    def load_session(self, session_id: str, cwd: str) -> str:
        result = self.transport.request("session/load", {
            "sessionId": session_id, "cwd": cwd, "mcpServers": []})
        sid = str((result or {}).get("sessionId") or session_id)
        self.session_info[sid] = result or {}
        return sid

    @staticmethod
    def _mode_option(config_options: Any) -> Optional[Dict[str, Any]]:
        for opt in config_options or []:
            if isinstance(opt, dict) and opt.get("id") == "mode" \
                    and opt.get("type") == "select":
                return opt
        return None

    def configure_mode(self, session_id: str, policy_mode: str) -> Optional[str]:
        raw = str(policy_mode or "").strip().lower()
        if not raw:
            return None
        desired = _MODE_VALUE_MAP.get(raw)
        if desired is None:
            raise AcpError(
                f"modo de permiso no reconocido por el puente: {raw}")
        info = self.session_info.get(str(session_id)) or {}
        option = self._mode_option(info.get("configOptions"))
        if option is None:
            raise AcpError("la sesion no expone configOption 'mode'")
        values = [v.get("value") for v in option.get("options") or []
                  if isinstance(v, dict)]
        if desired not in values:
            raise AcpError(
                f"modo '{desired}' no anunciado por la sesion")
        if str(option.get("currentValue") or "") == desired:
            return desired
        result = self.transport.request("session/set_config_option", {
            "sessionId": session_id, "configId": "mode",
            "value": desired})
        confirmed = self._mode_option(
            (result or {}).get("configOptions"))
        if confirmed is None \
                or str(confirmed.get("currentValue") or "") != desired:
            raise AcpError(
                "set_config_option no confirmo el modo solicitado")
        option["currentValue"] = desired
        return desired

    def prompt(self, session_id: str, text: str,
               idle_timeout: Optional[float] = None) -> Any:
        return self.transport.request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        }, timeout=PROMPT_RESPONSE_WAIT, idle_timeout=idle_timeout)

    def cancel(self, session_id: str) -> None:
        try:
            self.transport.notify(
                "session/cancel", {"sessionId": session_id})
        except Exception:
            pass


def default_log_sink(state_dir: Path) -> Callable[[str], None]:
    def sink(msg: str) -> None:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            with open(state_dir / "watcher.log", "a", encoding="utf-8") as fh:
                fh.write(redact_text(msg) + "\n")
        except OSError:
            pass
    return sink


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "SofiaDevinHealth/1.0"

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] not in ("/health", "/"):
            self.send_response(404)
            self.end_headers()
            return
        payload, status = self.server.snapshot()  # type: ignore[attr-defined]
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silencio: no ensuciar logs
        pass


def start_health_server(port: int, snapshot) -> Optional[Any]:
    """Servidor HTTP minimo (solo localhost) con /health. None si no puede."""
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), _HealthHandler)
    except OSError:
        return None
    httpd.snapshot = snapshot  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class Watcher:
    def __init__(self, policy, group, source, store,
                 spawn_worker=default_spawn_worker,
                 is_worker_alive=worker_alive,
                 now=time.time,
                 today=lambda: date.today().isoformat(),
                 devin_exists=None,
                 log=None):
        if group not in policy["groups"]:
            raise BridgeError(f"grupo desconocido: {group}")
        self.policy = policy
        self.group = group
        self.source = source
        self.store = store
        self.spawn_worker = spawn_worker
        self.is_worker_alive = is_worker_alive
        self.now = now
        self.today = today
        self.log = log or default_log_sink(Path(policy["state_root"]) / group)
        if devin_exists is None:
            exe = policy.get("devin_executable", "")
            devin_exists = lambda: bool(exe) and Path(exe).exists()
        self.devin_exists = devin_exists
        self._last_child = None
        self._last_poll_at: Optional[float] = None

    def health_snapshot(self):
        """(payload, http_status) para el endpoint de salud del puente."""
        state: Dict[str, Any] = {}
        try:
            state = self.store.load()
        except Exception:
            state = {}
        age = None if self._last_poll_at is None else self.now() - self._last_poll_at
        stale = age is None or age > HEALTH_STALE_SECONDS
        return ({
            "ok": not stale,
            "group": self.group,
            "last_poll_age_seconds": None if age is None else round(age, 1),
            "status": state.get("status"),
            "worker_alive": bool(self.is_worker_alive(self.group)),
            "pending": len(state.get("pending") or []),
        }, (503 if stale else 200))

    def poll_once(self) -> Dict[str, Any]:
        decision: Dict[str, Any] = {"spawn_day": None, "queued": 0,
                                    "early": None}
        with self.store.transaction() as state:
            self._account_locked(state)
            if state["source_reset"]:
                decision["early"] = {"status": "source_reset", "queued": 0}
            elif not state["initialized"]:
                hw = self.source.highwater()
                state["initialized"] = True
                state["cursor"] = hw
                decision["early"] = {"status": "initialized", "cursor": hw,
                                     "queued": 0}
            else:
                cursor = int(state["cursor"])
                rows = self.source.fetch(
                    cursor, int(self.policy.get("batch_size", 50)))
                if not rows:
                    max_id = self.source.highwater()
                    if 0 < max_id < cursor:
                        state["source_reset"] = True
                        state["status"] = "source_reset"
                        state["status_at"] = self.now()
                        state["last_error"] = (
                            f"max(occurrence.id)={max_id} < cursor={cursor}")
                        decision["early"] = {"status": "source_reset",
                                             "queued": 0}
                else:
                    day = self.today()
                    handled_last = cursor
                    for row in rows:
                        rid = int(row["occurrence_id"])
                        if route_event(self.policy, row) == self.group:
                            if len(state["pending"]) >= MAX_PENDING:
                                state["status"] = "pending_full"
                                state["status_at"] = self.now()
                                state["last_error"] = \
                                    "cola llena; cursor detenido"
                                break
                            state["pending"].append(event_from_row(row, day))
                            decision["queued"] += 1
                        handled_last = rid
                    state["cursor"] = handled_last
                    if decision["queued"]:
                        state["paused"] = False
                        if not state["circuit_open"] and (
                                state["status"] != "pending_full" or
                                len(state["pending"]) < MAX_PENDING):
                            state["status"] = "ok"
                            state["last_error"] = None
                if decision["early"] is None:
                    decision["spawn_day"] = self._spawn_day_locked(state)
        if decision["early"] is not None:
            return decision["early"]
        self._maybe_spawn(decision.get("spawn_day"))
        return {"status": "ok", "queued": decision["queued"]}

    def _spawn_day_locked(self, state) -> Optional[str]:
        if not state["pending"] or state["paused"] or state["circuit_open"]:
            return None
        days = [e["received_day"] for e in state["pending"]]
        return min(days) if days else None

    def _account_locked(self, state) -> None:
        alive = self.is_worker_alive(self.group)
        if state["inflight"] and state["inflight"].get("prompt_sent") \
                and not state["inflight"].get("uncertain") \
                and not alive:
            state["inflight"]["uncertain"] = True
            state["uncertain"] = True
            state["status"] = "uncertain"
            state["status_at"] = self.now()
            state["last_error"] = "worker murio durante un turno; entrega incierta"
        if state["worker_ready"] and not alive and state["last_spawn"]:
            state["worker_ready"] = False
            state["spawn_counted"] = True
            if state["pending"] and state["status"] == "ok":
                state["paused"] = True
                state["status"] = "paused"
                state["status_at"] = self.now()
                state["last_error"] = \
                    "consola cerrada por el usuario con reportes pendientes"
            return
        if state["last_spawn"] and not state["spawn_counted"]:
            if state["status"] in WORKER_FAIL_STATUS \
                    and state["status_at"] >= state["last_spawn"]:
                self._register_failure_locked(state)
            elif not state["worker_ready"] and not alive \
                    and self._last_child is not None \
                    and self._last_child.poll() == 0:
                state["spawn_counted"] = True
            elif not state["worker_ready"] and not alive \
                    and self.now() - state["last_spawn"] > SPAWN_GRACE_SECONDS:
                self._register_failure_locked(state)
            elif state["worker_ready"]:
                state["spawn_counted"] = True
                state["spawn_failures"] = 0

    def _register_failure_locked(self, state) -> None:
        state["spawn_counted"] = True
        state["spawn_failures"] += 1
        delay = min(300.0, 15.0 * (2 ** min(state["spawn_failures"], 5)))
        state["next_retry_after"] = self.now() + delay
        if state["spawn_failures"] >= MAX_SPAWN_FAILURES:
            state["circuit_open"] = True
            state["status"] = "spawn_circuit_open"
            state["status_at"] = self.now()
            state["last_error"] = "demasiados fallos de consola; pausado"

    def _maybe_spawn(self, day: Optional[str]) -> Optional[Any]:
        if day is None:
            return None
        if self.is_worker_alive(self.group):
            return None
        if not self.devin_exists():
            with self.store.transaction() as st:
                st["status"] = "agent_missing"
                st["status_at"] = self.now()
                st["last_error"] = "devin_executable no existe"
            return None
        with self.store.transaction() as st:
            if self.now() < float(st["next_retry_after"]):
                return None
            if st["last_spawn"] and not st["spawn_counted"] \
                    and not st["worker_ready"]:
                rc = self._last_child.poll() \
                    if self._last_child is not None else None
                if rc == 0:
                    st["spawn_counted"] = True
                else:
                    if rc is not None or self.now() - st["last_spawn"] \
                            >= SPAWN_GRACE_SECONDS:
                        self._register_failure_locked(st)
                    return None
            st["last_spawn"] = self.now()
            st["worker_ready"] = False
            st["spawn_counted"] = False
        try:
            proc = self.spawn_worker(self.policy, self.group, day)
        except Exception as exc:
            with self.store.transaction() as st:
                st["status"] = "spawn_error"
                st["status_at"] = self.now()
                st["last_error"] = f"no se pudo abrir consola: {redact_text(exc)[:300]}"
                self._register_failure_locked(st)
            return None
        self._last_child = proc
        return proc

    def run(self, poll_seconds=None, sleep=time.sleep,
            max_cycles=None) -> None:
        interval = float(poll_seconds or self.policy.get("poll_seconds", 15))
        port = (self.policy.get("health_ports") or {}).get(self.group)
        if port:
            if start_health_server(int(port), self.health_snapshot) is None:
                self.log(f"[watcher {self.group}] no se pudo abrir el health "
                         f"port {port}")
        cycles = 0
        while True:
            try:
                result = self.poll_once()
                self._last_poll_at = self.now()
                if result.get("status") not in ("idle", "ok"):
                    self.log(f"[watcher {self.group}] {result}")
            except (StateError, BridgeError) as exc:
                self.log(f"[watcher {self.group}] ERROR cerrado: {redact_text(exc)}")
                break
            except Exception as exc:
                self.log(f"[watcher {self.group}] error de ciclo: {redact_text(exc)}")
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            sleep(interval)


HELP_TEXT = (
    "Comandos: /salir cierra la consola, /estado muestra el estado, "
    "/detalle muestra el ultimo detalle tecnico registrado, "
    "/reintentar reenvia el lote incierto (manual), /ayuda muestra esto. "
    "Otro texto se envia como mensaje a Devin en esta misma sesion.")

_DEVIN_LEVEL_TOKEN_RE = re.compile(
    r"(?i)\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|PANIC)\b")
_DEVIN_ERR_LEVELS = ("ERROR", "FATAL", "PANIC")


class ConsoleWorker:
    def __init__(self, policy, group, day, store,
                 spawn_agent=default_spawn_agent,
                 stdin_queue=None, stdout=None,
                 now=time.time,
                 today=lambda: date.today().isoformat(),
                 poll_state_seconds=STATE_POLL_SECONDS,
                 verify_elevation_fn=verify_elevation,
                 mutex_factory=HeldMutex,
                 auth_fn=devin_authenticated,
                 title_fn=set_console_title):
        self.policy = policy
        self.group = group
        self.group_cfg = policy["groups"][group]
        self.day = day
        self.store = store
        self.spawn_agent = spawn_agent
        self.now = now
        self.today = today
        self.poll_state_seconds = poll_state_seconds
        self.verify_elevation_fn = verify_elevation_fn
        self.mutex_factory = mutex_factory
        self.auth_fn = auth_fn
        self.title_fn = title_fn
        self.out = stdout if stdout is not None else sys.stdout
        self.stdin_queue = stdin_queue or queue.Queue()
        self._owns_stdin = stdin_queue is None
        self._session: Optional[AcpSession] = None
        self._transport: Optional[AcpTransport] = None
        self._session_id: Optional[str] = None
        self._turn_active = False
        self._backlog_lock = threading.Lock()
        self._user_backlog: List[Optional[str]] = []
        self._permission_pending = threading.Event()
        self._stdin_eof = False
        self._permission_answer: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._mutex: Optional[HeldMutex] = None
        self._chunk_buf = ""
        self._pem_lines: Optional[List[str]] = None
        self._disconnect_notified = False
        self._eof_defer = False
        self.cancel_grace = 2.0
        self._out_lock = threading.Lock()
        self._diag_lines = deque(maxlen=50)
        self._last_error_diag: Optional[str] = None
        self._seen_diag_banner: set = set()
        self._seen_tool_status: set = set()
        self._active_event_ids: List[int] = []
        self._pending_sig = None
        self._dispatcher_started = False
        self._diag_logger = None
        self._ui_logger = None
        self._effective_mode: Optional[str] = None
        self._stall_retries = 0

    def _print(self, text: str = "") -> None:
        try:
            line = redact_text(text) + "\n"
            with self._out_lock:
                self.out.write(line)
                self.out.flush()
        except Exception:
            pass

    def _raw_write(self, text: str) -> None:
        try:
            with self._out_lock:
                self.out.write(text)
                self.out.flush()
        except Exception:
            pass

    def _rotating_logger(self, attr: str, filename: str):
        logger = getattr(self, attr)
        if logger is not None:
            return logger
        try:
            self.store.state_dir.mkdir(parents=True, exist_ok=True)
            logger = logging.getLogger(
                f"sofia-bridge-{filename}-{id(self)}")
            logger.propagate = False
            logger.setLevel(logging.INFO)
            handler = logging.handlers.RotatingFileHandler(
                str(self.store.state_dir / filename), maxBytes=1024 * 1024,
                backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            setattr(self, attr, logger)
        except Exception:
            setattr(self, attr, None)
            return None
        return logger

    def _devin_log(self, line: str) -> None:
        text = redact_text(str(line))[:1000]
        self._diag_lines.append(text)
        try:
            logger = self._rotating_logger(
                "_diag_logger", "devin-diagnostics.log")
            if logger is not None:
                logger.info(text)
        except Exception:
            pass
        match = _DEVIN_LEVEL_TOKEN_RE.search(text)
        if match and match.group(1).upper() in _DEVIN_ERR_LEVELS:
            self._last_error_diag = text
            if text not in self._seen_diag_banner:
                self._seen_diag_banner.add(text)
                self._print("Devin informó un problema técnico. "
                            "Puedes ver el detalle con /detalle.")

    def _ui_event(self, event: str, **fields: Any) -> None:
        try:
            logger = self._rotating_logger(
                "_ui_logger", "ui-events.jsonl")
            if logger is None:
                return
            rec = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "event": event, "group": self.group, "day": self.day,
                "session_id": self._session_id}
            rec.update(fields)
            logger.info(json.dumps(rec, ensure_ascii=False))
        except Exception:
            pass

    def _start_dispatchers(self) -> None:
        if self._dispatcher_started:
            return
        self._dispatcher_started = True
        if self._owns_stdin:
            threading.Thread(target=self._stdin_loop, daemon=True).start()
        threading.Thread(
            target=self._stdin_dispatch_loop, daemon=True).start()

    def _emit_line(self, line: str) -> None:
        if self._pem_lines is not None:
            self._pem_lines.append(line)
            if "-----END" in line and "PRIVATE KEY-----" in line:
                block = "\n".join(self._pem_lines)
                self._pem_lines = None
                self._raw_write(redact_text(block) + "\n")
            return
        if "-----BEGIN" in line and "PRIVATE KEY-----" in line:
            self._pem_lines = [line]
            return
        safe = redact_text(line)
        self._raw_write(safe + "\n")
        self._ui_event("agent_message", text=safe,
                       event_ids=list(self._active_event_ids))

    def _stream_write(self, text: str) -> None:
        self._chunk_buf += str(text)
        while "\n" in self._chunk_buf:
            line, self._chunk_buf = self._chunk_buf.split("\n", 1)
            self._emit_line(line)

    def _flush_stream(self) -> None:
        if self._pem_lines is not None:
            self._pem_lines = None
            self._chunk_buf = ""
            self._raw_write("[CLAVE_PRIVADA_REDACTADA]\n")
            self._ui_event("agent_message",
                           text="[CLAVE_PRIVADA_REDACTADA]",
                           event_ids=list(self._active_event_ids))
            return
        if self._chunk_buf:
            buf = self._chunk_buf
            self._chunk_buf = ""
            if "-----BEGIN" in buf and "PRIVATE KEY-----" in buf:
                head = buf.split("-----BEGIN", 1)[0]
                if head:
                    safe_head = redact_text(head)
                    self._raw_write(safe_head)
                    self._ui_event("agent_message", text=safe_head,
                                   event_ids=list(self._active_event_ids))
                self._raw_write("[CLAVE_PRIVADA_REDACTADA]")
                self._ui_event("agent_message",
                               text="[CLAVE_PRIVADA_REDACTADA]",
                               event_ids=list(self._active_event_ids))
            else:
                safe = redact_text(buf)
                self._raw_write(safe)
                self._ui_event("agent_message", text=safe,
                               event_ids=list(self._active_event_ids))

    def _stdin_loop(self) -> None:
        try:
            for line in sys.stdin:
                self.stdin_queue.put(line.rstrip("\r\n"))
        except Exception:
            pass
        self.stdin_queue.put(None)

    def _on_notification(self, msg: Dict[str, Any]) -> None:
        if msg.get("method") != "session/update":
            return
        update = (msg.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate") or update.get("type") or ""
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if text:
                self._stream_write(str(text))
        elif kind in ("tool_call", "tool_call_update"):
            title = str(update.get("title")
                        or update.get("toolCallId") or "herramienta")
            status = str(update.get("status") or "")
            key = (str(update.get("toolCallId") or title), status)
            if key in self._seen_tool_status:
                return
            self._seen_tool_status.add(key)
            if status in ("in_progress", "pending"):
                self._print(f"\nDevin está revisando: {title}")
            elif status == "completed":
                self._print(f"Acción terminada: {title}")
            elif status == "failed":
                self._print(f"Acción falló: {title}")
            elif status:
                self._print(f"Acción {status}: {title}")
            else:
                self._print(f"Devin está usando: {title}")

    def _on_request(self, msg: Dict[str, Any]) -> Any:
        if msg.get("method") == "session/request_permission":
            return self._ask_permission(msg.get("params") or {})
        raise AcpError(f"metodo entrante no soportado: {msg.get('method')}")

    def _permission_content_texts(self, content: Any) -> List[str]:
        texts = []

        def walk(node):
            if isinstance(node, dict):
                for key in ("text", "path"):
                    val = node.get(key)
                    if isinstance(val, str) and val.strip():
                        texts.append(val)
                for val in node.values():
                    if isinstance(val, (dict, list)):
                        walk(val)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, str) and node.strip():
                texts.append(node)

        walk(content)
        seen = set()
        out = []
        for t in texts:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _ask_permission(self, params: Dict[str, Any]) -> Dict[str, Any]:
        options = params.get("options") or []
        tool = params.get("toolCall") or {}
        title = "accion"
        if isinstance(tool, dict):
            title = tool.get("title") or tool.get("toolCallId") or "accion"
            content = tool.get("content")
            locations = tool.get("locations") or []
        else:
            content, locations = None, []
        self._ui_event("permission_requested",
                       title=redact_text(str(title)),
                       event_ids=list(self._active_event_ids))
        self._print("")
        self._print("*" * 60)
        self._print("*** Devin necesita tu autorización ***")
        self._print(f"Acción: {title}")
        for text in self._permission_content_texts(content):
            for line in str(text).splitlines() or [text]:
                self._print(f"  {line}")
        for loc in locations:
            if isinstance(loc, dict) and loc.get("path"):
                self._print(f"  ruta: {loc['path']}")
        numbered = []
        for idx, opt in enumerate(options, 1):
            name = opt.get("name") or opt.get("optionId") or f"opcion {idx}"
            numbered.append(opt.get("optionId") or name)
            self._print(f"  {idx}. {name}")
        self._print("Escribe el número y presiona Enter "
                    "(vacío o inválido = rechazar).")
        self._print("*" * 60)
        self._permission_answer = queue.Queue()
        self._permission_pending.set()
        answer = ""
        try:
            while True:
                try:
                    answer = self._permission_answer.get(timeout=1.0)
                    break
                except queue.Empty:
                    if self._stdin_eof or self._stop.is_set() or (
                            self._transport and self._transport.closed):
                        answer = ""
                        break
        finally:
            self._permission_pending.clear()
        selected = None
        try:
            idx = int((answer or "").strip())
            if 1 <= idx <= len(numbered):
                selected = numbered[idx - 1]
        except (TypeError, ValueError):
            selected = None
        if selected is None:
            self._print("Permiso rechazado.")
            reject_id = None
            for opt in options:
                kind = str(opt.get("kind") or "").lower()
                if "reject" in kind or "cancel" in kind:
                    reject_id = opt.get("optionId") or opt.get("name")
                    break
            if reject_id:
                self._ui_event("permission_resolved", optionId=reject_id,
                               outcome="selected",
                               event_ids=list(self._active_event_ids))
                return {"outcome": {"outcome": "selected", "optionId": reject_id}}
            self._ui_event("permission_resolved", optionId=None,
                           outcome="cancelled",
                           event_ids=list(self._active_event_ids))
            return {"outcome": {"outcome": "cancelled"}}
        self._print(f"Permiso: {selected}")
        self._ui_event("permission_resolved", optionId=selected,
                       outcome="selected",
                       event_ids=list(self._active_event_ids))
        return {"outcome": {"outcome": "selected", "optionId": selected}}

    def _mark_status(self, status: str, error: Optional[str] = None) -> None:
        try:
            with self.store.transaction() as state:
                state["status"] = status
                state["status_at"] = self.now()
                state["last_error"] = redact_text(error)[:300] if error else None
        except Exception:
            pass

    def _take_pending_events(self) -> List[Dict[str, Any]]:
        state = self.store.load()
        if state["uncertain"] or state["inflight"]:
            return []
        completed = set(state["completed"])
        out = []
        for e in state["pending"]:
            if e["received_day"] != self.day:
                continue
            if e["occurrence_id"] in completed:
                continue
            out.append(e)
            if len(out) >= int(self.policy.get("batch_size", 50)):
                break
        return out

    def run(self) -> int:
        try:
            self.verify_elevation_fn(self.group_cfg)
        except ElevationMismatch as exc:
            self._print(f"ERROR: {exc}")
            self._mark_status("elevation_mismatch", str(exc))
            return 3
        self._mutex = self.mutex_factory(worker_mutex_name(self.group))
        if not self._mutex.acquire():
            self._print("Ya hay una consola de este grupo abierta. Saliendo.")
            return 4
        try:
            return self._run_inner()
        finally:
            self._mutex.release()

    def _run_inner(self) -> int:
        title = self.group_cfg.get("title", "Sofia - Devin")
        self.title_fn(f"{title} - {self.day}")
        labels = {"local": "Apps de esta PC (administrador)",
                  "elenatec": "ElenaTec (solo commit, sin deploy)"}
        group_label = labels.get(self.group, title)
        self._print("=" * 60)
        self._print("  Sofía + Devin")
        self._print(f"  {group_label}")
        self._print(f"  Conversación del día {self.day}")
        self._print("  Escribí /ayuda para ver los comandos.")
        self._print("=" * 60)
        self._start_dispatchers()
        exe = self.policy.get("devin_executable", "")
        if not exe or not Path(exe).exists():
            self._print("ERROR: no se encontro el ejecutable de Devin CLI.")
            self._mark_status("agent_missing", "devin_executable no existe")
            return 5
        if not self.auth_fn(exe):
            self._print("ERROR: Devin CLI no esta autenticado. "
                        "Se detiene la activacion; avisar al usuario.")
            self._mark_status("auth_required", "devin auth status fallo")
            return 5
        try:
            st = self.store.load()
            sess = st["sessions"].get(self.day) or {}
            has_session = bool(sess.get("session_id"))
        except Exception:
            has_session = False
        if not has_session and not self._has_day_work():
            self._print("Sin reportes ni sesion guardada para este dia; "
                        "no se abre una sesion de Devin.")
            try:
                with self.store.transaction() as st2:
                    st2["worker_ready"] = True
            except Exception:
                pass
            return 0
        self._ui_event("console_ready")
        self._print("Conectando con Devin...")
        if not self._connect():
            return 5
        self._print(f"Sesión lista ({self._session_id}). Los reportes del día "
                    "llegan a esta conversación.")
        try:
            st = self.store.load()
            if st["uncertain"] or st["inflight"]:
                self._print("Hay un turno anterior sin confirmación. Los "
                            "avisos siguen guardados. No reenviaré nada "
                            "automáticamente; usa /reintentar solo si deseas "
                            "repetir ese lote.")
        except Exception:
            pass
        threading.Thread(target=self._queue_watch_loop, daemon=True).start()
        try:
            with self.store.transaction() as state:
                state["worker_ready"] = True
        except Exception:
            pass
        rc = self._main_loop()
        self._close_transport()
        return rc

    def _queue_watch_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(max(0.5, self.poll_state_seconds))
            try:
                state = self.store.load()
            except Exception:
                continue
            ids = {e.get("occurrence_id") for e in state["pending"]}
            if self._pending_sig is None:
                self._pending_sig = ids
                continue
            new_ids = ids - self._pending_sig
            self._pending_sig = ids
            if new_ids and self._turn_active \
                    and not self._permission_pending.is_set():
                self._print("\nSofía agregó nuevos avisos; se atenderán al "
                            "terminar el turno actual.")

    def _connect(self) -> bool:
        exe = self.policy.get("devin_executable", "")
        try:
            proc = self.spawn_agent(
                exe, str(self.policy.get("permission_mode", "")),
                self.group_cfg["cwd"])
        except Exception as exc:
            self._print(f"ERROR: no se pudo iniciar devin acp: {redact_text(exc)}")
            self._mark_status("agent_spawn_error", str(exc)[:300])
            return False
        self._seen_diag_banner = set()
        self._seen_tool_status = set()
        self._transport = AcpTransport(
            proc, on_notification=self._on_notification,
            on_request=self._on_request,
            stderr_sink=self._devin_log)
        self._session = AcpSession(self._transport)
        try:
            self._session.initialize()
        except Exception as exc:
            self._print(f"ERROR: fallo initialize de Devin ACP: {redact_text(exc)}")
            self._mark_status("agent_init_error", str(exc)[:300])
            self._close_transport()
            return False
        try:
            self._session_id = self._ensure_session()
        except BridgeError as exc:
            self._print(f"ERROR: {redact_text(exc)}")
            self._close_transport()
            return False
        if self._session_id is None:
            self._close_transport()
            return False
        try:
            effective = self._session.configure_mode(
                self._session_id, str(self.policy.get("permission_mode") or ""))
        except Exception as exc:
            self._print(f"ERROR: no se pudo fijar el modo de permisos de la "
                        f"sesión: {redact_text(exc)}")
            self._mark_status("permission_mode_error", str(exc)[:300])
            self._close_transport()
            return False
        if effective:
            self._effective_mode = effective
            self._ui_event("permission_mode_configured",
                           requested_mode=effective, effective_mode=effective)
            if effective == "bypass":
                self._print("Modo de sesión: /bypass")
            else:
                self._print(f"Modo de sesión: {effective}")
        self._disconnect_notified = False
        return True

    def _close_transport(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _stall_seconds(self) -> float:
        try:
            return float(self.policy.get("stall_seconds", STALL_SECONDS))
        except (TypeError, ValueError):
            return STALL_SECONDS

    def _recover_after_stall(self) -> bool:
        """Cierra la conexion colgada y reconecta la misma sesion guardada."""
        try:
            if self._session is not None and self._session_id:
                self._session.cancel(self._session_id)
        except Exception:
            pass
        self._close_transport()
        self._turn_active = False
        self._disconnect_notified = False
        if not self._connect():
            self._print("*** No se pudo reconectar con Devin tras el cuelgue. "
                        "Usa /reintentar o /salir. ***")
            self._mark_status("stall_reconnect_failed", "reconexion fallida")
            return False
        return True

    def _ensure_session(self) -> Optional[str]:
        cwd = self.group_cfg["cwd"]
        state = self.store.load()
        session = state["sessions"].get(self.day)
        if session and session.get("session_id"):
            sid = str(session["session_id"])
            if not self._session.load_supported():
                self._mark_status(
                    "manual_gap",
                    "sesion previa del dia pero agente sin loadSession")
                self._print("No se puede reanudar la sesion del dia; "
                            "recuperacion manual requerida.")
                return None
            try:
                new_sid = self._session.load_session(sid, cwd)
            except Exception as exc:
                self._mark_status("manual_gap", f"session/load fallo: {exc}")
                self._print("Fallo la reanudacion; no se crea reemplazo "
                            "automatico.")
                return None
            with self.store.transaction() as st:
                st["sessions"][self.day] = {
                    "session_id": new_sid,
                    "initial_prompt_sent": bool(
                        session.get("initial_prompt_sent"))}
            self._session_id = new_sid
            self._ui_event("session_ready", session_id=new_sid,
                           resumed=True)
            return new_sid
        try:
            sid = self._session.new_session(cwd)
        except Exception as exc:
            self._mark_status("session_error", f"session/new fallo: {exc}")
            raise BridgeError(f"no se pudo crear la sesion del dia: {exc}") from exc
        with self.store.transaction() as st:
            st["sessions"][self.day] = {
                "session_id": sid, "initial_prompt_sent": False}
        self._session_id = sid
        self._ui_event("session_ready", session_id=sid, resumed=False)
        return sid

    def _send_batch(self, events: List[Dict[str, Any]],
                    use_initial: bool) -> bool:
        if not events:
            return True
        event_ids = [e["occurrence_id"] for e in events]
        if use_initial:
            text = build_initial_prompt(
                self.group_cfg["_initial_template"], self.day, events)
        else:
            text = render_report(events, self.day)
        with self.store.transaction() as state:
            state["inflight"] = {
                "event_ids": event_ids, "prompt_sent": False,
                "uncertain": False, "use_initial": use_initial,
                "day": self.day, "stop_reason": None}
        self._print(f"\n--- Enviando {len(events)} reporte(s) a Devin ---")
        self._print(text)
        self._print("Devin está analizando este lote...")
        self._active_event_ids = list(event_ids)
        self._ui_event("prompt_started", event_ids=list(event_ids))
        try:
            with self.store.transaction() as state:
                if state["inflight"]:
                    state["inflight"]["prompt_sent"] = True
            self._turn_active = True
            result = self._session.prompt(
                self._session_id, text, idle_timeout=self._stall_seconds())
        except AcpStallError as exc:
            self._turn_active = False
            self._flush_stream()
            self._active_event_ids = []
            self._print("\n*** Devin dejo de responder (turno colgado). "
                        "Reinicio la conexion y reintento el lote. ***")
            self._ui_event("stall_recovered", phase="batch")
            recovered = self._recover_after_stall()
            if recovered and self._stall_retries < MAX_STALL_RETRIES:
                self._stall_retries += 1
                with self.store.transaction() as state:
                    state["inflight"] = None
                    state["uncertain"] = False
                return self._send_batch(events, use_initial)
            self._inflight_failed(exc)
            return False
        except Exception as exc:
            self._turn_active = False
            self._flush_stream()
            self._active_event_ids = []
            self._inflight_failed(exc)
            return False
        self._turn_active = False
        self._flush_stream()
        self._active_event_ids = []
        stop_reason = (result or {}).get("stopReason") if isinstance(result, dict) else None
        if stop_reason != "end_turn":
            if stop_reason == "max_tokens" \
                    and self._stall_retries < MAX_STALL_RETRIES:
                self._stall_retries += 1
                self._print("\n*** El turno se corto por limite de tokens "
                            "(max_tokens); reintento el lote. ***")
                self._ui_event("turn_retry", reason="max_tokens")
                with self.store.transaction() as state:
                    state["inflight"] = None
                    state["uncertain"] = False
                return self._send_batch(events, use_initial)
            self._inflight_incomplete(stop_reason)
            return False
        self._stall_retries = 0
        with self.store.transaction() as state:
            completed = set(state["completed"])
            completed.update(event_ids)
            state["completed"] = sorted(completed)[-MAX_COMPLETED:]
            state["pending"] = [
                e for e in state["pending"]
                if e["occurrence_id"] not in completed]
            state["inflight"] = None
            state["uncertain"] = False
            if use_initial:
                sess = state["sessions"].setdefault(self.day, {})
                sess["initial_prompt_sent"] = True
                sess["session_id"] = self._session_id
            state["status"] = "ok"
            state["status_at"] = self.now()
            state["last_error"] = None
        self._ui_event("prompt_completed", event_ids=list(event_ids),
                       stop_reason="end_turn")
        self._print("\nDevin terminó este lote. Sigo escuchando a Sofía.")
        return True

    def _inflight_failed(self, exc: Exception) -> None:
        with self.store.transaction() as state:
            if state["inflight"] and state["inflight"].get("prompt_sent"):
                state["uncertain"] = True
                state["inflight"]["uncertain"] = True
                state["status"] = "uncertain"
                state["status_at"] = self.now()
                state["last_error"] = "entrega incierta; /reintentar manual"
                self._print("\n*** Entrega incierta: el agente no confirmo el "
                            "turno. NO se reenvia automaticamente. "
                            "Usa /reintentar. ***")
            else:
                state["inflight"] = None
                state["status"] = "send_error"
                state["status_at"] = self.now()
                state["last_error"] = f"prompt no enviado: {redact_text(exc)[:300]}"
                self._print(f"\n*** Error antes del envio; lote conservado: "
                            f"{redact_text(exc)[:200]} ***")

    def _inflight_incomplete(self, stop_reason: Optional[str]) -> None:
        with self.store.transaction() as state:
            if state["inflight"]:
                state["inflight"]["uncertain"] = True
                state["inflight"]["stop_reason"] = stop_reason
            state["uncertain"] = True
            state["status"] = f"turn_incomplete:{stop_reason or 'sin_resultado'}"
            state["status_at"] = self.now()
            state["last_error"] = "turno sin end_turn; reportes retenidos"
        self._print(f"\n*** Turno incompleto (stopReason={stop_reason}). "
                    f"Reportes retenidos; usa /reintentar o revisa Devin. ***")

    def _stdin_dispatch_loop(self) -> None:
        while True:
            try:
                line = self.stdin_queue.get()
            except Exception:
                break
            if line is None:
                self._stdin_eof = True
                if self._turn_active:
                    self._request_stop()
                elif self._permission_pending.is_set():
                    self._permission_answer.put("")
                with self._backlog_lock:
                    self._user_backlog.append(None)
                continue
            if line.strip() == "/salir":
                self._request_stop()
                continue
            if self._permission_pending.is_set():
                self._permission_answer.put(line)
                continue
            with self._backlog_lock:
                self._user_backlog.append(line)

    def _request_stop(self) -> None:
        self._print("Cerrando la consola.")
        if self._permission_pending.is_set():
            self._permission_answer.put("")
        self._stop.set()
        if self._turn_active and self._session and self._session_id:
            self._print("Cancelando el turno en curso (session/cancel)...")
            self._session.cancel(self._session_id)
            threading.Thread(target=self._bound_cancel_wait,
                             daemon=True).start()

    def _bound_cancel_wait(self) -> None:
        time.sleep(self.cancel_grace)
        if self._stop.is_set() and self._transport is not None \
                and not self._transport.closed:
            self._transport.close()

    def _drain_backlog(self) -> None:
        while True:
            with self._backlog_lock:
                if not self._user_backlog:
                    return
                line = self._user_backlog.pop(0)
            self._process_user_line(line)
            if self._stop.is_set():
                return

    def _has_sendable(self) -> bool:
        try:
            state = self.store.load()
        except Exception:
            return False
        if state["uncertain"] or state["inflight"]:
            return False
        completed = set(state["completed"])
        return any(
            e["received_day"] == self.day
            and e["occurrence_id"] not in completed
            for e in state["pending"])

    def _process_user_line(self, line: Optional[str]) -> None:
        if line is None:
            if self._has_sendable():
                self._eof_defer = True
            else:
                self._stop.set()
            return
        cmd = line.strip()
        if not cmd:
            return
        if cmd == "/salir":
            self._request_stop()
            return
        if cmd == "/ayuda":
            self._print(HELP_TEXT)
            return
        if cmd == "/estado":
            try:
                state = self.store.load()
                pendientes_hoy = sum(
                    1 for e in state["pending"]
                    if e.get("received_day") == self.day
                    and e.get("occurrence_id")
                    not in set(state["completed"]))
                if state["uncertain"]:
                    situacion = ("hay un lote incierto retenido; "
                                 "usa /reintentar para reenviarlo")
                elif self._transport is None or self._transport.closed:
                    situacion = "desconectado de Devin; usa /reintentar o /salir"
                elif self._permission_pending.is_set():
                    situacion = "esperando tu autorización en una acción"
                elif self._turn_active or state["inflight"]:
                    situacion = "procesando un lote con Devin"
                else:
                    situacion = "esperando nuevos reportes de Sofía"
                self._print(
                    f"Estado: {situacion}. Pendientes de hoy: "
                    f"{pendientes_hoy}. Sesión: {self._session_id} "
                    f"(día {self.day}). Modo: "
                    f"{self._effective_mode or 'n/d'}.")
            except Exception as exc:
                self._print(f"No pude leer el estado local: "
                            f"{redact_text(exc)}")
            return
        if cmd == "/detalle":
            printed = False
            if self._last_error_diag:
                self._print("Último error técnico registrado (redactado):")
                self._print(f"  {self._last_error_diag}")
                printed = True
            if self._diag_lines:
                self._print("--- Últimos mensajes técnicos (redactados) ---")
                for dline in list(self._diag_lines)[-10:]:
                    self._print(f"  {dline}")
                printed = True
            if not printed:
                self._print("No hay detalle técnico registrado en esta sesión.")
            return
        if cmd == "/reintentar":
            self._retry_inflight()
            return
        if self._turn_active:
            self._print("(turno en curso; tu mensaje se enviara despues)")
            with self._backlog_lock:
                self._user_backlog.append(line)
            return
        if self._session is None or self._transport is None \
                or self._transport.closed:
            self._print("Sin conexion con Devin; usa /reintentar tras recuperarla "
                        "o /salir.")
            return
        attempts = 0
        while True:
            self._active_event_ids = []
            self._ui_event("prompt_started", event_ids=[])
            stalled = False
            try:
                self._turn_active = True
                self._print("\n--- Tu mensaje a Devin ---")
                result = self._session.prompt(
                    self._session_id, line, idle_timeout=self._stall_seconds())
                if isinstance(result, dict) \
                        and result.get("stopReason") == "end_turn":
                    self._ui_event("prompt_completed", event_ids=[],
                                   stop_reason="end_turn")
            except AcpStallError:
                stalled = True
            except Exception as exc:
                self._print(f"*** Error enviando mensaje: {redact_text(exc)[:200]} ***")
            finally:
                self._turn_active = False
                self._flush_stream()
                self._active_event_ids = []
            if not stalled:
                return
            self._print("\n*** Devin dejo de responder (turno colgado). "
                        "Reinicio la conexion y reenvio el mensaje. ***")
            self._ui_event("stall_recovered", phase="message")
            recovered = self._recover_after_stall()
            if not recovered or attempts >= MAX_STALL_RETRIES:
                self._print("*** No se pudo recuperar el turno. "
                            "Volve a enviar el mensaje o usa /salir. ***")
                return
            attempts += 1

    def _retry_inflight(self) -> None:
        state = self.store.load()
        inflight = state["inflight"]
        if not (state["uncertain"] and inflight):
            self._print("No hay lote incierto para reintentar.")
            return
        if self._transport is None or self._transport.closed:
            self._print("Reconectando con Devin (misma sesion guardada)...")
            if not self._connect():
                self._print("No se pudo reconectar; lote sigue retenido.")
                return
        events = [e for e in state["pending"]
                  if e["occurrence_id"] in set(inflight.get("event_ids", []))]
        with self.store.transaction() as st:
            st["inflight"] = None
            st["uncertain"] = False
        self._send_batch(events, use_initial=bool(inflight.get("use_initial")))

    def _has_day_work(self) -> bool:
        try:
            state = self.store.load()
            if state["inflight"] or state["uncertain"]:
                return True
            completed = set(state["completed"])
            return any(
                e["received_day"] == self.day
                and e["occurrence_id"] not in completed
                for e in state["pending"])
        except Exception:
            return True

    def _heal_truncated_turn(self) -> None:
        """Un turno cortado por max_tokens no es una entrega incierta: se
        descarta el inflight para que el worker lo reintente desde pending."""
        try:
            state = self.store.load()
        except Exception:
            return
        inflight = state.get("inflight") or {}
        if state.get("uncertain") and inflight.get("stop_reason") == "max_tokens":
            with self.store.transaction() as st:
                st["inflight"] = None
                st["uncertain"] = False
                st["status"] = "ok"
                st["status_at"] = self.now()
                st["last_error"] = None
            self._print("Turno previo cortado por max_tokens: lo reintento.")

    def _main_loop(self) -> int:
        self._start_dispatchers()
        self._heal_truncated_turn()
        try:
            while not self._stop.is_set():
                if self._eof_defer and not self._has_sendable():
                    break
                if self.today() != self.day and not self._has_day_work():
                    self._print("Cambio de dia: esta sesion pertenece a "
                                f"{self.day}. Cerrando sin interrumpir turnos.")
                    try:
                        with self.store.transaction() as st:
                            st["worker_ready"] = False
                            st["spawn_counted"] = True
                            st["last_spawn"] = 0.0
                    except Exception:
                        pass
                    break
                if self._transport and self._transport.closed:
                    self._handle_disconnect()
                    self._drain_backlog()
                    if self._stop.is_set():
                        break
                    time.sleep(self.poll_state_seconds)
                    continue
                if not self._turn_active:
                    self._drain_backlog()
                    if self._stop.is_set():
                        break
                    events = self._take_pending_events()
                    if events:
                        try:
                            sess = self.store.load()["sessions"].get(
                                self.day, {})
                            use_initial = not bool(
                                sess.get("initial_prompt_sent"))
                        except Exception:
                            use_initial = True
                        self._send_batch(events, use_initial=use_initial)
                        continue
                time.sleep(self.poll_state_seconds)
            if self._stop.is_set():
                try:
                    with self.store.transaction() as st:
                        if st["pending"] or st["uncertain"]:
                            st["paused"] = True
                            if st["status"] == "ok":
                                st["status"] = "paused"
                                st["status_at"] = self.now()
                except Exception:
                    pass
            return 0
        except Exception:
            self._print(tb_mod.format_exc()[-1500:])
            return 7

    def _handle_disconnect(self) -> None:
        if self._disconnect_notified:
            return
        self._disconnect_notified = True
        try:
            with self.store.transaction() as st:
                if st["inflight"] and st["inflight"].get("prompt_sent"):
                    st["uncertain"] = True
                    st["inflight"]["uncertain"] = True
                    st["status"] = "uncertain"
                    st["status_at"] = self.now()
                    self._print("\n*** Conexion con Devin cerrada durante un "
                                "turno: entrega incierta. Usa /reintentar para "
                                "recuperar manualmente o /salir. ***")
                elif not self._stop.is_set():
                    self._print("\n*** Conexion con Devin cerrada. Usa "
                                "/reintentar o /salir. ***")
        except Exception:
            pass


def _build_source(policy: Dict[str, Any]) -> OccurrenceSource:
    return OccurrenceSource(
        Path(policy["source_db"]),
        policy["_occurrences_sql"], policy["_highwater_sql"])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="sofia_devin_bridge")
    parser.add_argument("mode", choices=("watch", "worker"))
    parser.add_argument("--group", required=True)
    parser.add_argument("--day", default=None)
    parser.add_argument("--policy-dir", default=str(DEFAULT_POLICY_DIR))
    parser.add_argument("--state-root", default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(Path(args.policy_dir))
    except BridgeError as exc:
        print(f"[sofia-devin-bridge] {exc}", file=sys.stderr)
        return 2
    if args.group not in policy["groups"]:
        print(f"[sofia-devin-bridge] grupo desconocido: {args.group}",
              file=sys.stderr)
        return 2
    state_root = Path(args.state_root or policy["state_root"])
    store = StateStore(state_root / args.group)
    if args.mode == "watch":
        mutex = HeldMutex(watcher_mutex_name(args.group))
        if not mutex.acquire():
            print(f"[sofia-devin-bridge] ya hay un watcher de {args.group}",
                  file=sys.stderr)
            return 4
        try:
            source = _build_source(policy)
            watcher = Watcher(policy, args.group, source, store)
            try:
                if args.once:
                    watcher.poll_once()
                else:
                    watcher.run()
            except BridgeError as exc:
                print(f"[sofia-devin-bridge] {exc}", file=sys.stderr)
                return 2
            return 0
        finally:
            mutex.release()
    day = args.day or date.today().isoformat()
    worker = ConsoleWorker(policy, args.group, day, store)
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
