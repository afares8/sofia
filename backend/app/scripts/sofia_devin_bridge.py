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
import urllib.request
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
DEFAULT_POLICY_DIR = REPO_ROOT / ".devin" / "sofia-cli"

STATE_VERSION = 1
# Tope blando de la cola: al llenarse NO se congela el cursor (eso dejaba a un
# CRITICAL nuevo detras de cientos de WARNING viejos). Se desaloja el aviso de
# menor prioridad mas antiguo; CRITICAL/ERROR solo se descartan en el tope duro.
MAX_PENDING = 500
MAX_PENDING_HARD = 2000
LEVEL_PRIORITY = {"CRITICAL": 3, "ERROR": 2, "WARNING": 1}
# Un lote incierto (el agente no confirmo end_turn) se reintenta solo cada
# `retry_interval_minutes`; a los `escalate_after_attempts` fallos seguidos se
# avisa por WhatsApp + correo (repitiendo como mucho cada
# `escalation_repeat_minutes`) porque el CLI ya no esta entregando nada.
RETRY_INTERVAL_SECONDS = 600.0
ESCALATE_AFTER_ATTEMPTS = 3
ESCALATION_REPEAT_SECONDS = 6 * 3600.0
ALERT_URL_DEFAULT = "http://127.0.0.1:5180/api/bridge/escalation"
ALERT_TOKEN_FILE_DEFAULT = r"C:\ProgramData\ServerSofia\bridge-alert.token"
ALERT_HTTP_TIMEOUT = 10.0
CONTROL_RPC_TIMEOUT = 60
SESSION_LOAD_TIMEOUT = 180
PROMPT_RESPONSE_WAIT = 24 * 3600
STATE_POLL_SECONDS = 5.0
MAX_SPAWN_FAILURES = 5
SPAWN_GRACE_SECONDS = 15.0
MAX_COMPLETED = 1000
SQLITE_READ_ATTEMPTS = 3
SQLITE_READ_DELAY = 0.15
# Ventana de acumulacion: tras el primer aviso nuevo se espera este tiempo
# (policy: batch_hold_seconds) antes de mandar el lote a Devin, para que una
# rafaga de eventos relacionados llegue en UN solo reporte en vez de varios.
BATCH_HOLD_SECONDS = 60.0
# Con mas de `backlog_digest_threshold` avisos en cola se manda UN solo mensaje
# resumido (agrupado por issue) en vez de N lotes; policy: backlog_digest*.
DIGEST_MAX_CHARS = 32000
# La consola se cierra sola tras este tiempo sin trabajo (sesion guardada);
# policy: idle_close_minutes.
IDLE_CLOSE_MINUTES = 60.0
# Un WARNING/ERROR con la misma firma (servicio + mensaje normalizado) que ya
# se envio dentro de esta ventana NO se vuelve a mandar al CLI: se cuenta y se
# manda como maximo una "recidiva" cada `repeat_escalate_after` ocurrencias.
# policy: repeat_suppress_minutes / repeat_suppress_levels /
# repeat_suppress_minutes_by_level / repeat_escalate_after. Los CRITICAL no se
# suprimen por defecto, pero la policy puede incluirlos con una ventana corta
# (p. ej. 10 min) para que una rafaga de CRITICAL identicos no abra N turnos.
REPEAT_SUPPRESS_MINUTES = 240.0
REPEAT_SUPPRESS_LEVELS = ("WARNING", "ERROR")
REPEAT_ESCALATE_AFTER = 12
MAX_SIGNATURES = 2000
# Reglas de silencio temporal por servicio/mensaje (policy: mute_rules). Un
# aviso que coincide NO se encola ni se manda al CLI; se cuenta en state.muted.
# Las occurrences siguen en sofia.db (no se borra evidencia). `levels` por
# defecto excluye CRITICAL; `expires_at` (YYYY-MM-DD) desactiva la regla sola.
MUTE_RULE_DEFAULT_LEVELS = ("WARNING", "ERROR")
# Los niveles de digest se acumulan y se mandan en UN lote/digest cada esta
# ventana. CRITICAL sigue saliendo de inmediato.
# policy: digest_levels / digest_minutes / digest_fresh_session / digest_force.
DIGEST_MINUTES = 60.0
DIGEST_LEVELS = ("WARNING", "ERROR")
DIGEST_FRESH_SESSION = True
DIGEST_FORCE = True
# La conversacion del dia se renueva (session/new + prompt inicial) cuando
# supera este numero de lotes o esta edad, para no reenviar 400-800k tokens de
# historial en cada prompt. policy: session_recycle_prompts/_minutes.
SESSION_RECYCLE_PROMPTS = 80
SESSION_RECYCLE_MINUTES = 240.0
# Niveles que rompen la ventana de acumulacion y salen de inmediato.
URGENT_BYPASS_LEVELS = ("CRITICAL",)
# Si el agente ACP muere sin turno en curso, el worker sale limpio y el watcher
# lo reabre tras esta espera para retomar la sesion.
DISCONNECT_RETRY_SECONDS = 60.0
STATE_REPLACE_ATTEMPTS = 8
STATE_REPLACE_DELAY = 0.2
STATE_REPLACE_MAX_DELAY = 2.0
STALL_SECONDS = 900.0
MAX_STALL_RETRIES = 1
STALL_WAIT_SLICE = 0.25
HEALTH_STALE_SECONDS = 120.0
# El CLI ACP acumula contexto y puede retener varios GB aun inactivo. Entre
# turnos se recicla la conexion (session/load conserva la conversacion) cuando
# el commit del proceso supera este umbral; policy: agent_commit_limit_mb.
MAX_AGENT_COMMIT_MB = 4096
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
SW_SHOWMINNOACTIVE = getattr(subprocess, "SW_SHOWMINNOACTIVE", 7)
WORKER_FAIL_STATUS = {
    "agent_missing", "agent_spawn_error", "agent_init_error", "session_error",
    "elevation_mismatch", "auth_required", "manual_gap", "spawn_error",
    "permission_mode_error",
}


class BridgeError(Exception):
    pass


class StateError(BridgeError):
    pass


class StateLockError(StateError):
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


def _mute_expiry(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise BridgeError(f"mute_rules.expires_at invalido: {value!r}") from exc


def compile_mute_rules(raw: Any) -> List[Dict[str, Any]]:
    """Compila `policy.mute_rules`; un error de configuracion falla cerrado.

    Cada regla: {id, service_id?, pattern, levels?, issue_ids?, expires_at?,
    note?}. `pattern` es regex case-insensitive contra el mensaje.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise BridgeError("policy.mute_rules debe ser una lista")
    rules: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BridgeError(f"mute_rules[{index}] debe ser un objeto")
        pattern = str(item.get("pattern") or "")
        if not pattern:
            raise BridgeError(f"mute_rules[{index}] sin pattern")
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise BridgeError(
                f"mute_rules[{index}] regex invalido: {exc}") from exc
        levels = item.get("levels", MUTE_RULE_DEFAULT_LEVELS)
        if isinstance(levels, str):
            levels = [levels]
        if not isinstance(levels, (list, tuple)):
            raise BridgeError(f"mute_rules[{index}].levels debe ser lista")
        raw_ids = item.get("issue_ids") or []
        if not isinstance(raw_ids, list):
            raise BridgeError(f"mute_rules[{index}].issue_ids debe ser lista")
        try:
            issue_ids = {int(x) for x in raw_ids}
        except (TypeError, ValueError) as exc:
            raise BridgeError(
                f"mute_rules[{index}].issue_ids invalido") from exc
        service_id = item.get("service_id")
        rules.append({
            "id": str(item.get("id") or pattern),
            "service_id": str(service_id).lower() if service_id else None,
            "rx": rx,
            "levels": {str(x).upper() for x in levels},
            "issue_ids": issue_ids,
            "expires_on": _mute_expiry(item.get("expires_at")),
            "note": str(item.get("note") or ""),
        })
    return rules


def mute_rule_for(policy: Dict[str, Any], ev: Dict[str, Any],
                  today: Any = None) -> Optional[str]:
    """Id de la regla que silencia este aviso, o None."""
    rules = policy.get("_mute_rules") or []
    if not rules:
        return None
    if isinstance(today, str):
        try:
            day = date.fromisoformat(today[:10])
        except ValueError:
            day = date.today()
    else:
        day = today or date.today()
    service_id = str(ev.get("service_id") or "").lower()
    level = str(ev.get("level") or "").upper()
    issue_id = ev.get("issue_id")
    message = str(ev.get("message") or "")
    for rule in rules:
        expires = rule.get("expires_on")
        if expires is not None and day > expires:
            continue
        if rule["service_id"] and rule["service_id"] != service_id:
            continue
        if rule["levels"] and level not in rule["levels"]:
            continue
        if rule["issue_ids"] and issue_id not in rule["issue_ids"]:
            continue
        if rule["rx"].search(message):
            return rule["id"]
    return None


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
        "exclude_service_ids": {
            str(s).lower() for s in route.get("exclude_service_ids", [])},
    }
    policy["_levels"] = {str(l).upper() for l in policy.get("levels", [])}
    policy["_mute_rules"] = compile_mute_rules(policy.get("mute_rules"))
    return policy


def route_event(policy: Dict[str, Any], row: Dict[str, Any]) -> Optional[str]:
    level = str(row.get("level") or "").upper()
    if level not in policy["_levels"]:
        return None
    route = policy["_elenatec"]
    source = str(row.get("source") or "").lower()
    service_id = str(row.get("service_id") or "").lower()
    # Componentes del propio monitor (p.ej. droplet-ssh: el poll del monitor
    # docker de Sofia) son area local aunque su fuente sea "docker": el fix es
    # Sofia-side y lo aplica la sesion local de esta PC.
    if service_id in route["exclude_service_ids"]:
        return "local"
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

    def _with_retry(self, fn):
        last_exc: Optional[Exception] = None
        for attempt in range(SQLITE_READ_ATTEMPTS):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if attempt + 1 < SQLITE_READ_ATTEMPTS:
                    time.sleep(SQLITE_READ_DELAY * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    def highwater(self) -> int:
        def _op() -> int:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                row = conn.execute(self._hw_sql).fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()
        return self._with_retry(_op)

    def fetch(self, after_id: int, batch_size: int) -> List[Dict[str, Any]]:
        def _op() -> List[Dict[str, Any]]:
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
        return self._with_retry(_op)


def _initial_state() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION, "initialized": False, "cursor": 0,
        "sessions": {}, "pending": [], "inflight": None, "completed": [],
        "paused": False, "source_reset": False, "uncertain": False,
        "status": "ok", "status_at": 0.0, "last_error": None,
        "spawn_failures": 0, "next_retry_after": 0.0, "last_spawn": 0.0,
        "spawn_counted": False, "worker_ready": False, "circuit_open": False,
        "hold_until": 0.0, "pause_cursor": 0,
        "retry_attempts": 0, "retry_after": 0.0, "escalated_at": 0.0,
        "escalation_count": 0, "dropped": 0, "muted": 0,
        "sig_sent": {}, "warning_hold_until": 0.0,
    }


# Claves agregadas despues de la version 1 inicial: los state.json existentes
# no las traen y cargar debe seguir funcionando (se completan con el default).
_OPTIONAL_STATE_KEYS = {
    "hold_until", "pause_cursor", "retry_attempts", "retry_after",
    "escalated_at", "escalation_count", "dropped", "muted",
    "sig_sent", "warning_hold_until",
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
        missing = set(base) - set(state) - _OPTIONAL_STATE_KEYS
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
        ("hold_until", (int, float)), ("pause_cursor", int),
        ("retry_attempts", int), ("retry_after", (int, float)),
        ("escalated_at", (int, float)), ("escalation_count", int),
        ("dropped", int), ("muted", int), ("sig_sent", dict),
        ("warning_hold_until", (int, float)),
    )
    for key, typ in checks:
        if not isinstance(state[key], typ):
            raise StateError(f"campo de estado invalido: {key}")
    for sig, rec in state["sig_sent"].items():
        if not isinstance(sig, str) or not isinstance(rec, dict) \
                or not isinstance(rec.get("last"), (int, float)) \
                or not isinstance(rec.get("count"), int) \
                or not isinstance(rec.get("suppressed"), int):
            raise StateError("sig_sent invalido")
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


def remote_group_cfg(group_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Config del modo remoto de un grupo, o None si trabaja en local.

    Un grupo remoto ejecuta el agente ACP en otro host (por SSH) con el
    working tree de produccion como cwd. El commit queda en ese repo, que es
    el propio `origin`, asi que no hace falta push.
    """
    remote = group_cfg.get("remote")
    if isinstance(remote, dict) and remote.get("host"):
        return remote
    return None


def group_agent_cwd(group_cfg: Dict[str, Any]) -> str:
    remote = remote_group_cfg(group_cfg)
    if remote:
        return str(remote.get("cwd") or group_cfg["cwd"])
    return str(group_cfg["cwd"])


def group_devin_executable(policy: Dict[str, Any],
                           group_cfg: Dict[str, Any]) -> str:
    remote = remote_group_cfg(group_cfg)
    if remote:
        return str(remote.get("devin_executable")
                   or policy.get("devin_executable", ""))
    return str(policy.get("devin_executable", ""))


def _ssh_base(host: str, timeout: int = 20) -> List[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
            "-o", "ServerAliveInterval=30", host]


def verify_elevation(group_cfg: Dict[str, Any]) -> None:
    remote = remote_group_cfg(group_cfg)
    if remote:
        if not bool(remote.get("require_root", True)):
            return
        host = str(remote["host"])
        try:
            proc = subprocess.run(
                _ssh_base(host) + ["id", "-u"],
                stdin=subprocess.DEVNULL,
                capture_output=True, timeout=40)
        except Exception as exc:
            raise ElevationMismatch(
                f"ssh a {host} fallo: {redact_text(exc)[:200]}") from exc
        if proc.returncode != 0 or proc.stdout.strip() != b"0":
            raise ElevationMismatch(
                f"remoto {host}: se esperaba root (uid 0)")
        return
    expected = bool(group_cfg.get("elevated"))
    if is_user_admin() != expected:
        raise ElevationMismatch(
            f"elevacion esperada={expected}; ejecucion cancelada")


def devin_authenticated(executable: str) -> bool:
    try:
        proc = subprocess.run(
            [executable, "auth", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True, timeout=20)
        return proc.returncode == 0
    except Exception:
        return False


def devin_authenticated_remote(host: str, executable: str) -> bool:
    return devin_authenticated_remote_detail(host, executable)[0]


def devin_authenticated_remote_detail(
        host: str, executable: str) -> Tuple[bool, Optional[str]]:
    """`devin auth status` por SSH: (ok, detalle del fallo).

    El detalle importa: un timeout de ssh y un "no autenticado" real se veian
    igual (auth_required sin motivo), y el 503 del puente no dejaba rastro.
    IMPORTANTE: pasar stdin=DEVNULL porque _stdin_loop ya bloquea sobre el
    STD_INPUT_HANDLE de la consola en Windows y ssh.exe colisiona si lo hereda.
    """
    try:
        proc = subprocess.run(
            _ssh_base(host) + [executable, "auth", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True, timeout=40)
    except subprocess.TimeoutExpired:
        return False, "devin auth status remoto: ssh sin respuesta en 40 s"
    except Exception as exc:
        return False, f"devin auth status remoto: {redact_text(exc)[:160]}"
    if proc.returncode == 0:
        return True, None
    err = redact_text(proc.stderr.decode("utf-8", "replace")).strip()
    if not err:
        return False, f"devin auth status remoto rc={proc.returncode}"
    return False, f"devin auth status remoto rc={proc.returncode}: {err}"[:300]


def _auth_result(value: Any) -> Tuple[bool, Optional[str]]:
    """Normaliza el resultado de un auth_fn (bool o (ok, detalle))."""
    if isinstance(value, tuple):
        return bool(value[0]), value[1]
    return bool(value), None


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
            for attempt in range(STATE_REPLACE_ATTEMPTS):
                try:
                    os.replace(tmp, self.path)
                    return
                except OSError as exc:
                    if (getattr(exc, "winerror", None) not in (5, 32)
                            or attempt + 1 >= STATE_REPLACE_ATTEMPTS):
                        raise
                    time.sleep(min(STATE_REPLACE_DELAY * (2 ** attempt),
                                   STATE_REPLACE_MAX_DELAY))
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
                raise StateLockError("no se pudo adquirir el lock de estado")
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


def find_pending_duplicate(pending: List[Dict[str, Any]],
                           ev: Dict[str, Any],
                           exclude_ids: Optional[set] = None) -> Optional[Dict[str, Any]]:
    """Busca en la cola un evento del MISMO issue (misma familia de error).

    Una rafaga de lineas del mismo problema crea varias occurrences del mismo
    issue_id; mientras ese issue siga en la cola, las ocurrencias adicionales
    se agrupan (no se manda lo mismo varias veces). Un issue sin id o que ya
    fue despachado (ya no esta en pending) no se deduplica.
    """
    issue_id = ev.get("issue_id")
    if issue_id is None:
        return None
    for e in pending:
        if exclude_ids and e.get("occurrence_id") in exclude_ids:
            continue
        if e.get("issue_id") == issue_id:
            return e
    return None


def retry_interval_seconds(policy: Dict[str, Any]) -> float:
    """Espera entre reintentos automaticos de un lote incierto (10 min)."""
    try:
        minutes = float(policy.get("retry_interval_minutes", 10))
    except (TypeError, ValueError):
        minutes = 10.0
    return max(60.0, minutes * 60.0)


def _event_priority(ev: Dict[str, Any]) -> int:
    return LEVEL_PRIORITY.get(str(ev.get("level") or "").upper(), 1)


def _pick_eviction_victim(pending: List[Dict[str, Any]],
                          incoming: int) -> Optional[Dict[str, Any]]:
    """Aviso mas antiguo de MENOR prioridad que el entrante (o None)."""
    victim = None
    victim_prio = incoming
    for e in pending:
        prio = _event_priority(e)
        if prio < victim_prio:
            victim, victim_prio = e, prio
            if prio == 1:
                break
    return victim


def _oldest_with_priority(pending: List[Dict[str, Any]],
                          priority: int) -> Optional[Dict[str, Any]]:
    for e in pending:
        if _event_priority(e) <= priority:
            return e
    return None


def admit_event(state: Dict[str, Any], ev: Dict[str, Any]) -> str:
    """Encola un aviso sin congelar nunca el cursor.

    Devuelve "queued" | "evicted" (entro desalojando ruido) | "dropped" (se
    descarta el entrante porque no hay nada menos importante que sacar).

    Reglas: un CRITICAL/ERROR nuevo siempre entra (hasta el tope duro); el
    ruido WARNING se descarta cuando la cola esta llena de cosas igual de
    importantes, y refresca la cola desalojando el WARNING mas antiguo. Asi un
    CRITICAL de hoy no queda detras de 500 WARNING de ayer, que es lo que
    pasaba cuando la cola llena congelaba el cursor.
    """
    pending = state["pending"]
    incoming = _event_priority(ev)
    if len(pending) < MAX_PENDING:
        pending.append(ev)
        return "queued"
    victim = _pick_eviction_victim(pending, incoming)
    if victim is not None:
        pending.remove(victim)
        state["dropped"] = int(state.get("dropped") or 0) + 1
        pending.append(ev)
        return "evicted"
    if incoming <= 1:
        return "dropped"
    if len(pending) < MAX_PENDING_HARD:
        pending.append(ev)
        return "queued"
    victim = _oldest_with_priority(pending, incoming)
    if victim is not None:
        pending.remove(victim)
        state["dropped"] = int(state.get("dropped") or 0) + 1
        pending.append(ev)
        return "evicted"
    return "dropped"


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
        if ev.get("duplicates"):
            lines.append(
                f"  (+{ev['duplicates']} ocurrencia(s) del mismo issue "
                "agrupadas en esta ventana)")
        if ev["detail"]:
            lines.append("  detalle:")
            for dline in ev["detail"].splitlines():
                lines.append(f"    {dline}")
        lines.append("")
    return "\n".join(lines)


_LEVEL_ORDER = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2}
_VOLATILE_RE = re.compile(r"[0-9a-fA-F]{8,}|\d+")
_SPACES_RE = re.compile(r"\s+")


def _short_ts(value: Any) -> str:
    return str(value or "").replace("T", " ")[:19]


def _hhmmss(value: Any) -> str:
    return str(value or "").replace("T", " ")[11:19] or "?"


def _message_signature(message: Any) -> str:
    """Firma estable del mensaje: ignora ids, numeros y ruido variable."""
    text = _VOLATILE_RE.sub("#", redact_text(message or ""))
    return _SPACES_RE.sub(" ", text).strip()[:140]


def event_signature(ev: Dict[str, Any]) -> str:
    """Firma estable de un aviso: servicio + nivel + mensaje normalizado.

    Dos occurrences de la misma familia (p. ej. `slow request ... 4101 ms` y
    `... 4245 ms`) producen la misma firma, aunque cambien ids y numeros.
    """
    return "|".join((
        str(ev.get("service_id") or "").lower(),
        str(ev.get("level") or "").upper(),
        _message_signature(ev.get("message"))))


def _policy_minutes(policy: Dict[str, Any], key: str,
                    default: float) -> float:
    try:
        return max(0.0, float(policy.get(key, default)))
    except (TypeError, ValueError):
        return max(0.0, default)


def repeat_suppress_seconds(policy: Dict[str, Any],
                            level: Optional[str] = None) -> float:
    minutes = _policy_minutes(
        policy, "repeat_suppress_minutes", REPEAT_SUPPRESS_MINUTES)
    by_level = policy.get("repeat_suppress_minutes_by_level") or {}
    if level and isinstance(by_level, dict):
        try:
            override = by_level.get(str(level).upper())
            if override is not None:
                minutes = max(0.0, float(override))
        except (TypeError, ValueError):
            pass
    return minutes * 60.0


def repeat_suppress_levels(policy: Dict[str, Any]) -> set:
    raw = policy.get("repeat_suppress_levels", REPEAT_SUPPRESS_LEVELS)
    if isinstance(raw, str):
        raw = [raw]
    return {str(x).upper() for x in raw or []}


def repeat_escalate_after(policy: Dict[str, Any]) -> int:
    try:
        return max(0, int(policy.get(
            "repeat_escalate_after", REPEAT_ESCALATE_AFTER)))
    except (TypeError, ValueError):
        return REPEAT_ESCALATE_AFTER


def digest_seconds(policy: Dict[str, Any]) -> float:
    return _policy_minutes(
        policy, "digest_minutes", DIGEST_MINUTES) * 60.0


def digest_levels(policy: Dict[str, Any]) -> set:
    raw = policy.get("digest_levels", DIGEST_LEVELS)
    if isinstance(raw, str):
        raw = [raw]
    return {str(x).upper() for x in raw or []}


def digest_fresh_session(policy: Dict[str, Any]) -> bool:
    return bool(policy.get("digest_fresh_session", DIGEST_FRESH_SESSION))


def digest_force(policy: Dict[str, Any]) -> bool:
    return bool(policy.get("digest_force", DIGEST_FORCE))


def urgent_bypass_levels(policy: Dict[str, Any]) -> set:
    raw = policy.get("urgent_bypass_levels", URGENT_BYPASS_LEVELS)
    if isinstance(raw, str):
        raw = [raw]
    return {str(x).upper() for x in raw or []}


def prune_signatures_locked(state: Dict[str, Any], now: float,
                            window_seconds: float) -> None:
    """Acota `sig_sent` para que el state.json no crezca sin limite."""
    sigs = state.get("sig_sent") or {}
    cutoff = now - max(window_seconds * 2.0, 24 * 3600.0)
    for sig in [s for s, r in sigs.items()
                if float((r or {}).get("last") or 0.0) < cutoff]:
        sigs.pop(sig, None)
    if len(sigs) > MAX_SIGNATURES:
        keep = sorted(
            sigs.items(), key=lambda kv: float((kv[1] or {}).get("last") or 0.0),
            reverse=True)[:MAX_SIGNATURES]
        state["sig_sent"] = dict(keep)


def render_digest(events: List[Dict[str, Any]], day: str,
                  max_chars: int = DIGEST_MAX_CHARS) -> str:
    """Resumen de una cola grande en UN solo mensaje.

    Agrupa por issue_id, ordena por severidad y volumen, y conserva el id de
    occurrence para que el agente consulte el detalle completo en sofia.db.
    """
    if not events:
        return f"Reportes del dia {day}: sin avisos."
    levels: Dict[str, int] = {}
    services: Dict[str, int] = {}
    groups: Dict[Any, Dict[str, Any]] = {}
    for ev in events:
        levels[ev["level"]] = levels.get(ev["level"], 0) + 1
        services[ev["service_id"]] = services.get(ev["service_id"], 0) + 1
        key = (ev["service_id"], ev["level"],
               _message_signature(ev.get("message")))
        grp = groups.get(key)
        if grp is None:
            groups[key] = grp = {
                "count": 0, "dups": 0, "ev": ev, "ids": [],
                "issues": set(),
                "first": str(ev["timestamp"]), "last": str(ev["timestamp"])}
        grp["count"] += 1
        grp["dups"] += int(ev.get("duplicates") or 0)
        if ev.get("issue_id") is not None:
            grp["issues"].add(ev["issue_id"])
        if len(grp["ids"]) < 8:
            grp["ids"].append(ev["occurrence_id"])
        grp["first"] = min(grp["first"], str(ev["timestamp"]))
        grp["last"] = max(grp["last"], str(ev["timestamp"]))
    rows = sorted(
        groups.values(),
        key=lambda g: (_LEVEL_ORDER.get(g["ev"]["level"], 3),
                       -g["count"], g["last"]))
    stamps = [str(e["timestamp"]) for e in events]
    lines = [
        f"Reportes del dia {day} - RESUMEN DE COLA: {len(events)} avisos, "
        f"{len(rows)} issues distintos.",
        f"Ventana: {_short_ts(min(stamps))} a {_short_ts(max(stamps))} (UTC).",
        "Por nivel: " + ", ".join(
            f"{k} {levels[k]}" for k in
            sorted(levels, key=lambda k: _LEVEL_ORDER.get(k, 3))),
        "Por servicio: " + ", ".join(
            f"{k} {v}" for k, v in
            sorted(services.items(), key=lambda kv: -kv[1])),
        "",
        "Avisos agrupados por servicio, nivel y mensaje (orden: CRITICAL, "
        "ERROR, WARNING). El detalle completo de cada aviso esta en la tabla "
        "occurrences de sofia.db, id = occurrence; consultala read-only.",
        "",
    ]
    omitted = 0
    for grp in rows:
        ev = grp["ev"]
        extra = f" (+{grp['dups']} agrupadas)" if grp["dups"] else ""
        ids = ", ".join(str(i) for i in sorted(grp["ids"]))
        if grp["count"] > len(grp["ids"]):
            ids += f", +{grp['count'] - len(grp['ids'])} mas"
        service = ev["service_name"] or ev["service_id"]
        span = f"{_hhmmss(grp['first'])}-{_hhmmss(grp['last'])}"
        if ev["level"] == "WARNING":
            # Ruido repetitivo: una linea, sin ids (se consultan en sofia.db).
            block = [
                f"- [WARNING] {service} x{grp['count']}{extra} {span}: "
                f"{redact_text(ev['message'])[:120]}"]
        else:
            issues = len(grp["issues"])
            block = [
                f"- [{ev['level']}] {service} x{grp['count']}{extra} "
                f"{span} ({issues} issue(s))",
                f"  occurrences: {ids}",
                f"  mensaje: {redact_text(ev['message'])[:200]}",
            ]
            detail = (ev.get("detail") or "").strip()
            if detail and ev["level"] == "CRITICAL":
                block.append(
                    f"  detalle: {redact_text(detail.splitlines()[0])[:160]}")
        block.append("")
        if sum(len(x) + 1 for x in lines) + sum(len(x) + 1 for x in block) \
                > max_chars:
            omitted += grp["count"]
            continue
        lines.extend(block)
    if omitted:
        lines.append(
            f"... {omitted} aviso(s) omitidos por tamano; consultalos en "
            "sofia.db por rango de occurrence.")
    return "\n".join(lines)


def build_initial_prompt(template: str, day: str,
                         events: List[Dict[str, Any]],
                         report: Optional[str] = None) -> str:
    body = report if report is not None else render_report(events, day)
    return template.replace("{day}", day).replace("{report}", body)


def _worker_stderr_sink(policy: Dict[str, Any], group: str):
    """Archivo donde el worker vuelca su stderr (tracebacks).

    Sin esto, un worker que muere antes de marcar estado no deja ninguna
    evidencia: la consola se cierra y el watcher solo cuenta el fallo.
    """
    try:
        log_dir = Path(policy["state_root"]) / group
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "worker.err.log"
        try:
            if path.stat().st_size > 1_048_576:
                os.replace(path, log_dir / "worker.err.log.1")
        except OSError:
            pass
        return open(path, "a", encoding="utf-8", errors="replace")
    except Exception:
        return None


def default_spawn_worker(policy: Dict[str, Any], group: str,
                         day: str) -> subprocess.Popen:
    py = policy.get("python_executable") or sys.executable
    cmd = [py, str(SCRIPT_PATH), "worker", "--group", group, "--day", day,
           "--policy-dir", str(policy["_dir"])]
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = SW_SHOWMINNOACTIVE
    # IMPORTANTE: NO pasar stderr/stdout/stdin a Popen con CREATE_NEW_CONSOLE
    # desde pythonw.exe. En Windows, pasar cualquier handle activa
    # STARTF_USESTDHANDLES y copia STD_INPUT_HANDLE/STD_OUTPUT_HANDLE invalidos
    # de pythonw al hijo (OSError [Errno 22] en stdout y EOF inmediato en stdin).
    # El worker redirige su propio sys.stderr a worker.err.log al iniciar.
    return subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE,
                            startupinfo=startupinfo)


def default_spawn_agent(executable: str, mode: str,
                        cwd: str) -> subprocess.Popen:
    env = dict(os.environ)
    # Menos ruido en el stderr del CLI (y por tanto en devin-diagnostics.log):
    # el descubrimiento de skills y la revalidacion de config remota son
    # INFO/WARN periodicos sin valor diagnostico.
    env.setdefault(
        "RUST_LOG",
        "info,chisel_agent::skills_loading=warn,"
        "windsurf_api_client::remote_config=error")
    argv = [executable]
    if mode:
        env["DEVIN_PERMISSION_MODE"] = mode
        argv += ["--permission-mode", mode]
    argv.append("acp")
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0, cwd=cwd or None, env=env)


def default_spawn_agent_remote(host: str, executable: str,
                               mode: str) -> subprocess.Popen:
    """Arranca `devin acp` en el host remoto y habla ACP por stdio de ssh.

    El agente corre con el cwd del grupo (p. ej. /root/tec), asi que sus
    herramientas editan el codigo de produccion y sus commits quedan en el
    repo que es `origin`. No se envia DEVIN_PERMISSION_MODE: el modo se fija
    con el flag y se confirma luego por RPC.
    """
    argv = _ssh_base(host) + [executable]
    if mode:
        argv += ["--permission-mode", mode]
    argv.append("acp")
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0)


def process_commit_bytes(pid: Optional[int]) -> Optional[int]:
    """Commit privado del proceso (stdlib, solo Windows). None si no se pudo."""
    if os.name != "nt" or not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.PrivateUsage)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


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
        load_timeout = max(CONTROL_RPC_TIMEOUT, SESSION_LOAD_TIMEOUT)
        result = self.transport.request("session/load", {
            "sessionId": session_id, "cwd": cwd, "mcpServers": []},
            timeout=load_timeout,
            idle_timeout=load_timeout)
        sid = str((result or {}).get("sessionId") or session_id)
        self.session_info[sid] = result or {}
        return sid

    @staticmethod
    def _select_option(config_options: Any,
                       option_id: str) -> Optional[Dict[str, Any]]:
        for opt in config_options or []:
            if isinstance(opt, dict) and opt.get("id") == option_id \
                    and opt.get("type") == "select":
                return opt
        return None

    @staticmethod
    def _mode_option(config_options: Any) -> Optional[Dict[str, Any]]:
        return AcpSession._select_option(config_options, "mode")

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

    def configure_model(self, session_id: str,
                        policy_model: str) -> Optional[str]:
        """Fija el modelo de la sesion si la policy lo pide explicitamente.

        Vacio = usar el default del CLI. Si la policy fija un modelo que la
        sesion no anuncia, falla cerrado en vez de usar otro en silencio.
        """
        raw = str(policy_model or "").strip()
        if not raw:
            return None
        info = self.session_info.get(str(session_id)) or {}
        option = self._select_option(info.get("configOptions"), "model")
        if option is None:
            raise AcpError("la sesion no expone configOption 'model'")
        values = [v.get("value") for v in option.get("options") or []
                  if isinstance(v, dict)]
        if raw not in values:
            raise AcpError(f"modelo '{raw}' no anunciado por la sesion")
        if str(option.get("currentValue") or "") == raw:
            return raw
        result = self.transport.request("session/set_config_option", {
            "sessionId": session_id, "configId": "model",
            "value": raw})
        confirmed = self._select_option(
            (result or {}).get("configOptions"), "model")
        if confirmed is None \
                or str(confirmed.get("currentValue") or "") != raw:
            raise AcpError(
                "set_config_option no confirmo el modelo solicitado")
        option["currentValue"] = raw
        return raw

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


class BridgeAlertSender:
    """Aviso de escalado por WhatsApp + correo a traves del backend de Sofia.

    El puente es stdlib-only y no importa `app.*`: manda un POST al endpoint
    local `/api/bridge/escalation`, que reutiliza `whatsapp_service` y
    `email_alert_service` (con sus cooldowns y su cola de reintentos).
    """

    def __init__(self, policy: Dict[str, Any], group: str,
                 opener: Optional[Callable[..., Any]] = None,
                 token_reader: Optional[Callable[[], str]] = None):
        self.policy = policy
        self.group = group
        self.url = str(policy.get("alert_url") or ALERT_URL_DEFAULT)
        self.token_file = str(
            policy.get("alert_token_file") or ALERT_TOKEN_FILE_DEFAULT)
        self._opener = opener or urllib.request.urlopen
        self._token_reader = token_reader or self._read_token

    def _read_token(self) -> str:
        try:
            return Path(self.token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def send(self, payload: Dict[str, Any]) -> bool:
        token = self._token_reader()
        if not token:
            return False
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Bridge-Token": token})
        try:
            with self._opener(req, timeout=ALERT_HTTP_TIMEOUT) as resp:
                return 200 <= int(getattr(resp, "status", 0)) < 300
        except Exception:
            return False


class Watcher:
    def __init__(self, policy, group, source, store,
                 spawn_worker=default_spawn_worker,
                 is_worker_alive=worker_alive,
                 now=time.time,
                 today=lambda: date.today().isoformat(),
                 devin_exists=None,
                 log=None,
                 alert_sender=None):
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
            if remote_group_cfg(policy["groups"][group]):
                # El ejecutable vive en el host remoto; el worker valida
                # ssh/auth al arrancar y falla cerrado.
                devin_exists = lambda: True
            else:
                exe = policy.get("devin_executable", "")
                devin_exists = lambda: bool(exe) and Path(exe).exists()
        self.devin_exists = devin_exists
        self.alert_sender = alert_sender or BridgeAlertSender(policy, group)
        self._last_child = None
        self._last_poll_at: Optional[float] = None
        self._policy_mtime: Optional[float] = None

    def _reload_policy_if_changed(self) -> None:
        """Recarga policy.json cuando cambia (mute rules, digest, batch).

        Antes habia que reiniciar el watcher para aplicar una regla de
        silencio, y reiniciar la tarea desde dentro de una sesion mata al
        propio agente a mitad de turno. Las claves de alerta (alert_url,
        token) siguen leyendose al arrancar.
        """
        policy_dir = Path(str(self.policy.get("_dir") or ""))
        path = policy_dir / "policy.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if self._policy_mtime is None:
            self._policy_mtime = mtime
            return
        if mtime == self._policy_mtime:
            return
        try:
            fresh = load_policy(policy_dir)
        except BridgeError as exc:
            self._policy_mtime = mtime
            self.log(f"policy.json invalido; sigo con la anterior: {exc}")
            return
        self._policy_mtime = mtime
        self.policy = fresh
        self.log("policy.json recargado en caliente")

    def health_snapshot(self):
        """(payload, http_status) para el endpoint de salud del puente."""
        state: Dict[str, Any] = {}
        try:
            state = self.store.load()
        except Exception:
            state = {}
        age = None if self._last_poll_at is None else self.now() - self._last_poll_at
        stale = age is None or age > HEALTH_STALE_SECONDS
        status = state.get("status")
        worker_ok = bool(self.is_worker_alive(self.group))
        # `uncertain` es transitorio: murio un worker con un turno enviado y el
        # puente lo reintenta solo (10 min). NO es DOWN; si se reporta 503 el
        # monitor (con auto_restore) reinicia el puente encima del reintento y
        # alarga el problema. `paused`, `circuit_open` y `source_reset` si
        # requieren mirada humana.
        blocked = bool(state.get("paused") or state.get("circuit_open")
                       or state.get("source_reset"))
        ok = not stale and not blocked and (
            status in ("ok", "uncertain")
            or (status == "pending_full" and worker_ok))
        return ({
            "ok": ok,
            "group": self.group,
            "last_poll_age_seconds": None if age is None else round(age, 1),
            "status": status,
            "worker_alive": worker_ok,
            "uncertain": bool(state.get("uncertain")),
            "paused": bool(state.get("paused")),
            "pending": len(state.get("pending") or []),
            "muted": int(state.get("muted") or 0),
        }, (200 if ok else 503))

    def poll_once(self) -> Dict[str, Any]:
        self._reload_policy_if_changed()
        decision: Dict[str, Any] = {"spawn_day": None, "queued": 0,
                                    "deduped": 0, "dropped": 0, "evicted": 0,
                                    "suppressed": 0, "muted": 0,
                                    "has_more": False, "early": None}
        escalation: Optional[Dict[str, Any]] = None
        with self.store.transaction() as state:
            self._account_locked(state)
            # Silencios ya encolados (p. ej. al agregar la regla) no deben
            # llegar al CLI aunque el watcher se haya reiniciado despues.
            decision["muted"] += self._sweep_muted_locked(state)
            escalation = self._maybe_escalate_locked(state)
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
                batch_limit = int(self.policy.get("batch_size", 50))
                saw_routed = False
                rows = self.source.fetch(cursor, batch_limit)
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
                    had_pending = bool(state["pending"])
                    held_levels = digest_levels(self.policy)
                    had_digest = any(
                        e.get("level") in held_levels for e in state["pending"])
                    handled_last = cursor
                    inflight_ids = set(
                        (state.get("inflight") or {}).get("event_ids") or [])
                    for row in rows:
                        rid = int(row["occurrence_id"])
                        if route_event(self.policy, row) == self.group:
                            saw_routed = True
                            ev = event_from_row(row, day)
                            muted = mute_rule_for(self.policy, ev, day)
                            queued_now = False
                            duplicate = None if muted else find_pending_duplicate(
                                state["pending"], ev, exclude_ids=inflight_ids)
                            if muted:
                                state["muted"] = int(
                                    state.get("muted") or 0) + 1
                                decision["muted"] += 1
                            elif duplicate is not None:
                                duplicate["duplicates"] = int(
                                    duplicate.get("duplicates") or 0) + 1
                                decision["deduped"] += 1
                            elif self._suppress_signature_locked(state, ev):
                                decision["suppressed"] += 1
                            else:
                                outcome = admit_event(state, ev)
                                if outcome == "dropped":
                                    decision["dropped"] += 1
                                else:
                                    queued_now = True
                                    decision["queued"] += 1
                                    self._mark_digest_hold_locked(
                                        state, ev, reset=not had_digest)
                                if outcome == "evicted":
                                    decision["evicted"] += 1
                            if queued_now and ev["level"] in \
                                    urgent_bypass_levels(self.policy):
                                # Un CRITICAL nuevo no espera la ventana de digest.
                                state["hold_until"] = 0.0
                        handled_last = rid
                    state["cursor"] = handled_last
                    prune_signatures_locked(
                        state, self.now(), repeat_suppress_seconds(self.policy))
                    if len(state["pending"]) >= MAX_PENDING \
                            and not state["uncertain"]:
                        state["status"] = "pending_full"
                        state["status_at"] = self.now()
                        state["last_error"] = (
                            "cola llena: se desaloja ruido, el cursor avanza")
                    if decision["queued"]:
                        state["paused"] = False
                        state["pause_cursor"] = 0
                        self._arm_hold_locked(state, had_pending)
                        if state["uncertain"]:
                            if state["status"] in ("ok", "paused"):
                                state["status"] = "uncertain"
                        elif not state["circuit_open"] and (
                                state["status"] != "pending_full" or
                                len(state["pending"]) < MAX_PENDING):
                            state["status"] = "ok"
                            state["last_error"] = None
                    if (len(rows) >= batch_limit
                            and len(state["pending"]) < MAX_PENDING_HARD
                            and not state["circuit_open"]):
                        decision["has_more"] = True
                if decision["early"] is None:
                    if not any(e.get("level") in digest_levels(self.policy)
                               for e in state["pending"]):
                        # Un lote anterior pudo dejar la ventana armada sin
                        # avisos de digest pendientes (p. ej. al migrar).
                        state["warning_hold_until"] = 0.0
                    if (state["status"] == "pending_full"
                            and len(state["pending"]) < MAX_PENDING
                            and not state["circuit_open"]):
                        state["status"] = (
                            "uncertain" if state["uncertain"] else "ok")
                        state["status_at"] = self.now()
                        if not state["uncertain"]:
                            state["last_error"] = None
                    self._heal_stale_pause_locked(state, new_notice=saw_routed)
                    self._heal_manual_gap_locked(state)
                    decision["spawn_day"] = self._spawn_day_locked(state)
        if escalation is not None:
            if not self._send_escalation(escalation):
                with self.store.transaction() as st:
                    st["escalated_at"] = (
                        self.now() - self._escalation_repeat() + 600.0)
        if decision["early"] is not None:
            return decision["early"]
        self._maybe_spawn(decision.get("spawn_day"))
        return {"status": "ok", "queued": decision["queued"],
                "deduped": decision["deduped"],
                "dropped": decision["dropped"],
                "evicted": decision["evicted"],
                "suppressed": decision["suppressed"],
                "muted": decision["muted"],
                "has_more": decision["has_more"]}

    def _sweep_muted_locked(self, state) -> int:
        """Quita de la cola los avisos que ahora caen en una regla de silencio.

        No toca `inflight`: un lote ya enviado al CLI no se puede retirar.
        """
        pending = state.get("pending") or []
        if not pending or not self.policy.get("_mute_rules"):
            return 0
        day = self.today()
        keep = []
        removed = 0
        for ev in pending:
            if mute_rule_for(self.policy, ev, day):
                removed += 1
            else:
                keep.append(ev)
        if not removed:
            return 0
        state["pending"] = keep
        state["muted"] = int(state.get("muted") or 0) + removed
        if not any(e.get("level") in digest_levels(self.policy)
                   for e in keep):
            state["warning_hold_until"] = 0.0
        return removed

    def _suppress_signature_locked(self, state, ev) -> bool:
        """True si esta firma ya se envio hace poco y toca suprimirla.

        Solo aplica a los niveles configurados (WARNING por defecto). Cada
        `repeat_escalate_after` ocurrencias suprimidas se deja pasar una para
        que una recidiva persistente no quede invisible.
        """
        window = repeat_suppress_seconds(self.policy, ev["level"])
        if window <= 0 or ev["level"] not in repeat_suppress_levels(
                self.policy):
            return False
        rec = (state.get("sig_sent") or {}).get(event_signature(ev))
        if not rec:
            return False
        if self.now() - float(rec.get("last") or 0.0) >= window:
            return False
        rec["suppressed"] = int(rec.get("suppressed") or 0) + 1
        escalate = repeat_escalate_after(self.policy)
        if escalate > 0 and rec["suppressed"] >= escalate:
            rec["suppressed"] = 0
            return False
        return True

    def _mark_digest_hold_locked(self, state, ev, reset: bool = False) -> None:
        """Arma la ventana de digest si el nivel se acumula para el lote."""
        if ev["level"] not in digest_levels(self.policy):
            return
        window = digest_seconds(self.policy)
        if window <= 0:
            return
        hold = float(state.get("warning_hold_until") or 0.0)
        if reset or hold <= self.now():
            state["warning_hold_until"] = self.now() + window

    def _hold_seconds(self) -> float:
        try:
            return float(self.policy.get("batch_hold_seconds",
                                         BATCH_HOLD_SECONDS))
        except (TypeError, ValueError):
            return BATCH_HOLD_SECONDS

    def _arm_hold_locked(self, state, had_pending: bool) -> None:
        """Arma la ventana de acumulacion cuando arranca un lote nuevo.

        Si la cola ya tenia eventos (ventana en curso o backlog que espera
        worker), no se extiende la espera: la ventana vence igual.
        """
        hold = self._hold_seconds()
        if hold <= 0 or had_pending:
            return
        if float(state.get("hold_until") or 0.0) <= self.now():
            state["hold_until"] = self.now() + hold

    def _heal_stale_pause_locked(self, state, new_notice: bool = False) -> None:
        """Levanta la pausa por consola cerrada cuando llego un reporte nuevo
        despues de pausar (occurrence_id > pause_cursor).

        No depende de que el poll actual haya encolado algo: si el aviso se
        encolo en un ciclo anterior y la pausa no se limpio, el watcher se
        recupera solo en el siguiente ciclo.

        `new_notice` cubre los avisos del grupo que no llegan a `pending`
        (muteados, deduplicados o suprimidos por firma): tambien son reportes
        nuevos y sin ellos la pausa quedaba permanente.
        """
        if not state["paused"] or state["circuit_open"]:
            return
        cursor = int(state.get("pause_cursor") or 0)
        if not cursor:
            return
        if new_notice or any(int(e.get("occurrence_id") or 0) > cursor
                             for e in state["pending"]):
            state["paused"] = False
            state["pause_cursor"] = 0
            state["status"] = "uncertain" if state["uncertain"] else "ok"
            state["status_at"] = self.now()
            if not state["uncertain"]:
                state["last_error"] = None

    def _auto_resume_minutes(self) -> float:
        try:
            return max(0.0, float(self.policy.get("auto_resume_minutes", 15)))
        except (TypeError, ValueError):
            return 15.0

    def _heal_manual_gap_locked(self, state) -> None:
        """Un `manual_gap` no puede durar para siempre.

        Caso real 2026-11-07: `session/load` fallo, el estado quedo en
        manual_gap + paused con 500 pendientes y el grupo estuvo ~12 h sin
        triaje hasta que alguien lo miro. Si hay trabajo y la marca tiene mas
        de `auto_resume_minutes`, se levanta la pausa y el siguiente ciclo
        abre sesion (con el fallback a sesion nueva de `_ensure_session`).
        """
        if state.get("status") != "manual_gap" or state["circuit_open"]:
            return
        if not state["pending"]:
            return
        minutes = self._auto_resume_minutes()
        if minutes <= 0:
            return
        since = float(state.get("status_at") or 0.0)
        if since and self.now() - since < minutes * 60.0:
            return
        state["paused"] = False
        state["pause_cursor"] = 0
        state["status"] = "uncertain" if state["uncertain"] else "ok"
        state["status_at"] = self.now()
        state["last_error"] = None

    def _spawn_day_locked(self, state) -> Optional[str]:
        if not state["pending"] or state["paused"] or state["circuit_open"]:
            return None
        # Si solo hay avisos de digest esperando su ventana, no hace falta
        # abrir el CLI todavia: el watcher lo abre cuando vence la ventana o
        # cuando entra un CRITICAL.
        held_levels = digest_levels(self.policy)
        if float(state.get("warning_hold_until") or 0.0) > self.now() \
                and all(e.get("level") in held_levels
                        for e in state["pending"]):
            return None
        # Un backlog de dias anteriores no debe abrir la consola de ese dia
        # (nacio, trabajo 2 minutos y murio al reiniciar el watcher). Los
        # avisos viejos se reagrupan en el dia actual para que el reporte y la
        # sesion sean los de hoy.
        today = self.today()
        stale = [e for e in state["pending"]
                 if str(e.get("received_day") or "") < today]
        if stale:
            for e in stale:
                e["received_day"] = today
        days = [e["received_day"] for e in state["pending"]]
        return min(days) if days else None

    def _account_locked(self, state) -> None:
        alive = self.is_worker_alive(self.group)
        if state["inflight"] and state["inflight"].get("prompt_sent") \
                and not state["inflight"].get("uncertain") \
                and not alive:
            state["inflight"]["uncertain"] = True
            state["uncertain"] = True
            state["retry_attempts"] = int(state.get("retry_attempts") or 0) + 1
            state["retry_after"] = self.now() + retry_interval_seconds(
                self.policy)
            state["status"] = "uncertain"
            state["status_at"] = self.now()
            state["last_error"] = (
                "worker murio durante un turno; entrega incierta; "
                f"reintento automatico {state['retry_attempts']}")
        if state["worker_ready"] and not alive and state["last_spawn"]:
            state["worker_ready"] = False
            state["spawn_counted"] = True
            if state["pending"] and state["status"] == "ok":
                state["paused"] = True
                state["status"] = "paused"
                state["status_at"] = self.now()
                state["pause_cursor"] = int(state["cursor"])
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

    def _escalate_after(self) -> int:
        try:
            return max(1, int(self.policy.get(
                "escalate_after_attempts", ESCALATE_AFTER_ATTEMPTS)))
        except (TypeError, ValueError):
            return ESCALATE_AFTER_ATTEMPTS

    def _escalation_repeat(self) -> float:
        try:
            minutes = float(self.policy.get(
                "escalation_repeat_minutes", 360))
        except (TypeError, ValueError):
            minutes = 360.0
        return max(60.0, minutes * 60.0)

    def _maybe_escalate_locked(self, state) -> Optional[Dict[str, Any]]:
        """Payload de escalado si el lote lleva demasiados reintentos fallidos."""
        if not state.get("uncertain") or not state.get("inflight"):
            return None
        attempts = int(state.get("retry_attempts") or 0)
        if attempts < self._escalate_after():
            return None
        now = self.now()
        last = float(state.get("escalated_at") or 0.0)
        if last and now - last < self._escalation_repeat():
            return None
        state["escalated_at"] = now
        state["escalation_count"] = int(
            state.get("escalation_count") or 0) + 1
        return {
            "group": self.group,
            "status": state.get("status"),
            "attempts": attempts,
            "escalation": int(state["escalation_count"]),
            "error": state.get("last_error"),
            "pending": len(state.get("pending") or []),
            "cursor": int(state.get("cursor") or 0),
            "dropped": int(state.get("dropped") or 0),
            "retry_interval_minutes": int(
                retry_interval_seconds(self.policy) // 60),
        }

    def _send_escalation(self, payload: Dict[str, Any]) -> bool:
        try:
            ok = bool(self.alert_sender.send(payload))
        except Exception:
            ok = False
        if ok:
            self.log("escalado enviado por WhatsApp + correo "
                     f"(intento {payload.get('attempts')}, "
                     f"escalado {payload.get('escalation')})")
        else:
            self.log("escalado NO enviado (backend/token); se reintenta "
                     f"en 10 min (intento {payload.get('attempts')})")
        return ok

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
            if float(st.get("hold_until") or 0.0) > self.now():
                return None
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
            has_more = False
            try:
                result = self.poll_once()
                self._last_poll_at = self.now()
                has_more = bool(result.get("has_more"))
                if result.get("status") not in ("idle", "ok"):
                    self.log(f"[watcher {self.group}] {result}")
            except StateLockError as exc:
                self.log(f"[watcher {self.group}] error de ciclo: {redact_text(exc)}")
            except (StateError, BridgeError) as exc:
                self.log(f"[watcher {self.group}] ERROR cerrado: {redact_text(exc)}")
                raise
            except Exception as exc:
                self.log(f"[watcher {self.group}] error de ciclo: {redact_text(exc)}")
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            sleep(min(0.05, interval) if has_more else interval)


HELP_TEXT = (
    "Comandos: /salir cierra la consola, /estado muestra el estado, "
    "/detalle muestra el ultimo detalle tecnico registrado, "
    "/reintentar reenvia ahora el lote incierto (si no, se reintenta solo "
    "cada 10 min), /ayuda muestra esto. "
    "Otro texto se envia como mensaje a Devin en esta misma sesion.")

_DEVIN_LEVEL_TOKEN_RE = re.compile(
    r"(?i)\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|PANIC)\b")
_DEVIN_ERR_LEVELS = ("ERROR", "FATAL", "PANIC")
# Ruido del CLI que no aporta a un diagnostico: descubrimiento de skills cada
# 15 s, timeouts de revalidacion de config remota y esperas internas de stop.
# Se descarta antes de escribir devin-diagnostics.log (rotaba 1 MiB cada pocas
# horas solo con estas lineas).
_DEVIN_NOISE_RE = re.compile(
    r"skills discovery:|remote config revalidation failed|"
    r"Waiting for stop token", re.IGNORECASE)


class ConsoleWorker:
    def __init__(self, policy, group, day, store,
                 spawn_agent=default_spawn_agent,
                 spawn_agent_remote=default_spawn_agent_remote,
                 stdin_queue=None, stdout=None,
                 now=time.time,
                 today=lambda: date.today().isoformat(),
                 poll_state_seconds=STATE_POLL_SECONDS,
                 verify_elevation_fn=verify_elevation,
                 mutex_factory=HeldMutex,
                 auth_fn=devin_authenticated,
                 auth_remote_fn=devin_authenticated_remote_detail,
                 title_fn=set_console_title,
                 commit_fn=process_commit_bytes):
        self.policy = policy
        self.group = group
        self.group_cfg = policy["groups"][group]
        self.day = day
        self.store = store
        self.spawn_agent = spawn_agent
        self.spawn_agent_remote = spawn_agent_remote
        self.commit_fn = commit_fn
        self.now = now
        self.today = today
        self.poll_state_seconds = poll_state_seconds
        self.verify_elevation_fn = verify_elevation_fn
        self.mutex_factory = mutex_factory
        self.auth_fn = auth_fn
        self.auth_remote_fn = auth_remote_fn
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
        self._loading_session = False
        self._recycled_since_turn = False
        self._rotation_disabled = False

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
        if _DEVIN_NOISE_RE.search(text):
            return
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
        if sys.stdin is None:
            return
        try:
            for line in sys.stdin:
                self.stdin_queue.put(line.rstrip("\r\n"))
        except OSError:
            # Handle de stdin invalido (p. ej. consola desacoplada): no enviar
            # None para no disparar un falso EOF que cierre/pause el worker.
            return
        except Exception:
            pass
        self.stdin_queue.put(None)

    def _on_notification(self, msg: Dict[str, Any]) -> None:
        if msg.get("method") != "session/update":
            return
        if self._loading_session or not self._turn_active:
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
        if float(state.get("hold_until") or 0.0) > self.now():
            # Ventana de acumulacion activa: esperar a que llegue todo el lote.
            return []
        completed = set(state["completed"])
        available = [
            e for e in state["pending"]
            if e["received_day"] == self.day
            and e["occurrence_id"] not in completed]
        # Los niveles de digest se acumulan para un solo lote; CRITICAL sigue
        # en tiempo real. Al vencer la ventana, el lote incluye todo.
        if float(state.get("warning_hold_until") or 0.0) > self.now():
            held = digest_levels(self.policy)
            available = [e for e in available if e["level"] not in held]
        # Lo urgente primero: un CRITICAL nuevo no espera detras del ruido
        # acumulado (dentro de la misma prioridad se respeta el orden de llegada).
        available.sort(
            key=lambda e: (-_event_priority(e), e["occurrence_id"]))
        # Backlog grande: se manda completo (hasta MAX_PENDING) en un solo
        # mensaje resumido en vez de N lotes de batch_size.
        if self._digest_enabled() \
                and len(available) > self._digest_threshold():
            return available[:MAX_PENDING]
        return available[:int(self.policy.get("batch_size", 50))]

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
        remote = remote_group_cfg(self.group_cfg)
        exe = group_devin_executable(self.policy, self.group_cfg)
        if remote:
            host = str(remote["host"])
            if not exe:
                self._print("ERROR: modo remoto sin devin_executable.")
                self._mark_status("agent_missing", "remoto sin ejecutable")
                return 5
            ok, detail = _auth_result(self.auth_remote_fn(host, exe))
            if not ok:
                self._print("ERROR: Devin CLI remoto no esta autenticado. "
                            "Se detiene la activacion; avisar al usuario.")
                self._mark_status("auth_required",
                                  detail or "devin auth status remoto fallo")
                return 5
        else:
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
                retry_at = float(st.get("retry_after") or 0.0)
                attempts = int(st.get("retry_attempts") or 0)
                if retry_at > self.now():
                    falta = int((retry_at - self.now()) // 60) + 1
                    self._print("Hay un turno anterior sin confirmación "
                                f"(intento {attempts}). Los avisos siguen "
                                f"guardados; reintento automático en ~{falta} "
                                "min. /reintentar lo hace ahora mismo.")
                else:
                    self._print("Hay un turno anterior sin confirmación; "
                                "reintento automático en curso.")
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
        remote = remote_group_cfg(self.group_cfg)
        mode = str(self.policy.get("permission_mode", ""))
        try:
            if remote:
                proc = self.spawn_agent_remote(
                    str(remote["host"]),
                    group_devin_executable(self.policy, self.group_cfg),
                    mode)
            else:
                proc = self.spawn_agent(
                    group_devin_executable(self.policy, self.group_cfg),
                    mode, group_agent_cwd(self.group_cfg))
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
        model = str(self.policy.get("model") or "").strip()
        if model:
            try:
                effective_model = self._session.configure_model(
                    self._session_id, model)
            except Exception as exc:
                self._print(f"ERROR: no se pudo fijar el modelo de la sesión: "
                            f"{redact_text(exc)}")
                self._mark_status("model_error", str(exc)[:300])
                self._close_transport()
                return False
            if effective_model:
                self._ui_event("model_configured", model=effective_model)
                self._print(f"Modelo: {effective_model}")
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

    def _digest_enabled(self) -> bool:
        return bool(self.policy.get("backlog_digest", True))

    def _digest_threshold(self) -> int:
        try:
            return max(1, int(self.policy.get(
                "backlog_digest_threshold", 50)))
        except (TypeError, ValueError):
            return 50

    def _digest_max_chars(self) -> int:
        try:
            return max(2000, int(self.policy.get(
                "backlog_digest_max_chars", DIGEST_MAX_CHARS)))
        except (TypeError, ValueError):
            return DIGEST_MAX_CHARS

    def _idle_close_seconds(self) -> float:
        try:
            return max(0.0, float(self.policy.get(
                "idle_close_minutes", IDLE_CLOSE_MINUTES)) * 60.0)
        except (TypeError, ValueError):
            return IDLE_CLOSE_MINUTES * 60.0

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

    def _agent_commit_limit(self) -> int:
        try:
            mb = float(self.policy.get("agent_commit_limit_mb",
                                       MAX_AGENT_COMMIT_MB))
        except (TypeError, ValueError):
            mb = MAX_AGENT_COMMIT_MB
        return int(mb * 1024 * 1024)

    def _should_recycle_agent(self) -> bool:
        """True si el ACP inactivo retiene mas commit que el limite."""
        if self._turn_active or self._permission_pending.is_set():
            return False
        if self._recycled_since_turn or self._has_inflight_work():
            return False
        if self._transport is None or self._transport.closed:
            return False
        pid = getattr(self._transport._proc, "pid", None)
        commit = self.commit_fn(pid)
        return commit is not None and commit > self._agent_commit_limit()

    def _recycle_agent(self) -> bool:
        """Recicla el proceso ACP entre turnos conservando la sesion."""
        self._print("\nMemoria del agente alta; reciclo la conexion "
                    "(la conversacion se conserva).")
        self._ui_event("agent_recycled")
        self._close_transport()
        self._turn_active = False
        self._disconnect_notified = False
        if not self._connect():
            self._mark_status("agent_recycle_failed", "reconexion fallida")
            self._print("*** No se pudo reciclar el agente; la sesion "
                        "sigue guardada. ***")
            return False
        self._recycled_since_turn = True
        return True

    def _ensure_session(self) -> Optional[str]:
        cwd = group_agent_cwd(self.group_cfg)
        want_remote = remote_group_cfg(self.group_cfg) is not None
        state = self.store.load()
        session = state["sessions"].get(self.day)
        if session and session.get("session_id") \
                and bool(session.get("remote")) == want_remote:
            sid = str(session["session_id"])
            reason = None
            if not self._session.load_supported():
                reason = "agente sin loadSession"
            else:
                try:
                    self._loading_session = True
                    new_sid = self._session.load_session(sid, cwd)
                except Exception as exc:
                    reason = f"session/load fallo: {exc}"
                finally:
                    self._loading_session = False
            if reason:
                # Auto-recuperacion: una sesion que ya no se puede reanudar
                # (expirada, CLI actualizado, agente sin loadSession) no debe
                # dejar el puente en manual_gap hasta que alguien lo mire.
                # Se abre una sesion nueva del dia y se reenvia el prompt
                # inicial con el siguiente lote; el historial previo queda
                # solo en sessions.db.
                self._print(f"No se pudo reanudar la sesion ({reason}); "
                            "abro una nueva y sigo.")
                self._ui_event("session_load_failed", session_id=sid,
                               error=redact_text(reason)[:200])
                sid = self._session.new_session(cwd)
                with self.store.transaction() as st:
                    st["sessions"][self.day] = {
                        "session_id": sid, "initial_prompt_sent": False,
                        "prompts_sent": 0, "started_at": self.now(),
                        "remote": want_remote, "recovered_from": session.get(
                            "session_id")}
                self._session_id = sid
                self._mark_status("ok", None)
                self._ui_event("session_ready", session_id=sid,
                               resumed=False, recovered=True)
                return sid
            with self.store.transaction() as st:
                renewed = dict(session)
                renewed["session_id"] = new_sid
                renewed["remote"] = want_remote
                renewed["initial_prompt_sent"] = bool(
                    session.get("initial_prompt_sent"))
                renewed.setdefault("prompts_sent", 0)
                renewed.setdefault("started_at", self.now())
                st["sessions"][self.day] = renewed
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
                "session_id": sid, "initial_prompt_sent": False,
                "prompts_sent": 0, "started_at": self.now(),
                "remote": want_remote}
        self._session_id = sid
        self._ui_event("session_ready", session_id=sid, resumed=False)
        return sid

    def _session_recycle_prompts(self) -> int:
        try:
            return max(0, int(self.policy.get(
                "session_recycle_prompts", SESSION_RECYCLE_PROMPTS)))
        except (TypeError, ValueError):
            return SESSION_RECYCLE_PROMPTS

    def _session_recycle_minutes(self) -> float:
        return _policy_minutes(
            self.policy, "session_recycle_minutes", SESSION_RECYCLE_MINUTES)

    def _should_rotate_session(self) -> bool:
        """True si la conversacion del dia ya acumulo demasiado contexto."""
        if self._rotation_disabled:
            return False
        prompts_limit = self._session_recycle_prompts()
        minutes = self._session_recycle_minutes()
        if prompts_limit <= 0 and minutes <= 0:
            return False
        try:
            sess = self.store.load()["sessions"].get(self.day) or {}
        except Exception:
            return False
        if not sess.get("session_id"):
            return False
        if prompts_limit > 0 and int(
                sess.get("prompts_sent") or 0) >= prompts_limit:
            return True
        started = float(sess.get("started_at") or 0.0)
        return bool(minutes > 0 and started
                    and self.now() - started >= minutes * 60.0)

    def _rotate_session(self) -> bool:
        """Abre una sesion nueva del dia para resetear el contexto acumulado.

        El proximo lote se manda con el prompt inicial (instrucciones), asi el
        agente no arrastra cientos de miles de tokens de historial.
        """
        if self._session is None:
            return False
        try:
            sid = self._session.new_session(group_agent_cwd(self.group_cfg))
        except Exception as exc:
            self._mark_status("session_rotate_error",
                              f"session/new fallo: {exc}")
            self._print(f"\n*** No se pudo renovar la sesión: "
                        f"{redact_text(exc)[:200]} ***")
            return False
        try:
            effective = self._session.configure_mode(
                sid, str(self.policy.get("permission_mode") or ""))
        except Exception as exc:
            self._mark_status("permission_mode_error", str(exc)[:300])
            self._print(f"\n*** La sesión nueva no confirmó el modo: "
                        f"{redact_text(exc)[:200]} ***")
            return False
        model = str(self.policy.get("model") or "").strip()
        if model:
            try:
                self._session.configure_model(sid, model)
            except Exception as exc:
                self._mark_status("model_error", str(exc)[:300])
                self._print(f"\n*** La sesión nueva no confirmó el modelo: "
                            f"{redact_text(exc)[:200]} ***")
                return False
        with self.store.transaction() as st:
            st["sessions"][self.day] = {
                "session_id": sid, "initial_prompt_sent": False,
                "prompts_sent": 0, "started_at": self.now(),
                "remote": remote_group_cfg(self.group_cfg) is not None}
        self._session_id = sid
        if effective:
            self._effective_mode = effective
        self._ui_event("session_rotated", session_id=sid)
        self._print("\nRenuevo la conversación del día para no arrastrar todo "
                    "el historial (contexto alto). La sesión nueva recibe las "
                    "instrucciones y el próximo lote.")
        return True

    def _send_batch(self, events: List[Dict[str, Any]],
                    use_initial: bool) -> bool:
        if not events:
            return True
        event_ids = [e["occurrence_id"] for e in events]
        digest = (self._digest_enabled()
                  and len(events) > self._digest_threshold()) \
            or (digest_force(self.policy) and len(events) > 1)
        body = render_digest(events, self.day, self._digest_max_chars()) \
            if digest else render_report(events, self.day)
        if use_initial:
            text = build_initial_prompt(
                self.group_cfg["_initial_template"], self.day, events,
                report=body)
        else:
            text = body
        with self.store.transaction() as state:
            state["inflight"] = {
                "event_ids": event_ids, "prompt_sent": False,
                "uncertain": False, "use_initial": use_initial,
                "day": self.day, "stop_reason": None}
        if digest:
            self._print(f"\n--- Enviando un resumen de {len(events)} aviso(s) "
                        f"a Devin (un solo mensaje) ---")
        else:
            self._print(f"\n--- Enviando {len(events)} reporte(s) a Devin ---")
        self._print(text)
        self._print("Devin está analizando este lote...")
        self._active_event_ids = list(event_ids)
        self._ui_event("prompt_started", event_ids=list(event_ids),
                       digest=digest)
        try:
            with self.store.transaction() as state:
                if state["inflight"]:
                    state["inflight"]["prompt_sent"] = True
            self._turn_active = True
            self._recycled_since_turn = False
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
        now = self.now()
        with self.store.transaction() as state:
            completed = set(state["completed"])
            completed.update(event_ids)
            state["completed"] = sorted(completed)[-MAX_COMPLETED:]
            state["pending"] = [
                e for e in state["pending"]
                if e["occurrence_id"] not in completed]
            state["inflight"] = None
            state["uncertain"] = False
            self._clear_retry_locked(state)
            sess = state["sessions"].setdefault(self.day, {})
            if use_initial:
                sess["initial_prompt_sent"] = True
                sess["session_id"] = self._session_id
                sess["started_at"] = now
                sess["prompts_sent"] = 1
            else:
                sess["session_id"] = self._session_id
                sess.setdefault("started_at", now)
                sess["prompts_sent"] = int(sess.get("prompts_sent") or 0) + 1
            for e in events:
                sig = event_signature(e)
                rec = state["sig_sent"].setdefault(
                    sig, {"last": 0.0, "suppressed": 0, "count": 0})
                rec["last"] = now
                rec["count"] = int(rec.get("count") or 0) + 1
                rec["suppressed"] = 0
            prune_signatures_locked(
                state, now, repeat_suppress_seconds(self.policy))
            held = digest_levels(self.policy)
            window = digest_seconds(self.policy)
            old_hold = float(state.get("warning_hold_until") or 0.0)
            if window > 0 and any(e["level"] in held
                                  for e in state["pending"]):
                # Lo que llego mientras se enviaba espera la proxima ventana,
                # pero un envio CRITICAL no debe extender una ventana activa.
                if old_hold <= now:
                    state["warning_hold_until"] = now + window
            else:
                state["warning_hold_until"] = 0.0
            state["status"] = "ok"
            state["status_at"] = now
            state["last_error"] = None
        self._ui_event("prompt_completed", event_ids=list(event_ids),
                       stop_reason="end_turn", digest=digest)
        self._print("\nDevin terminó este lote. Sigo escuchando a Sofía.")
        return True

    def _inflight_failed(self, exc: Exception) -> None:
        detail = redact_text(exc)[:200]
        with self.store.transaction() as state:
            if state["inflight"] and state["inflight"].get("prompt_sent"):
                attempts = self._schedule_retry_locked(state)
                state["uncertain"] = True
                state["inflight"]["uncertain"] = True
                state["status"] = "uncertain"
                state["status_at"] = self.now()
                state["last_error"] = (
                    f"entrega incierta ({detail}); reintento automatico "
                    f"{attempts} en {int(self._retry_interval() // 60)} min"
                    if detail else "entrega incierta; reintento automatico")
                self._print(f"\n*** Entrega incierta ({detail}): el agente no "
                            "confirmo el turno. Reintento automatico #"
                            f"{attempts} en "
                            f"{int(self._retry_interval() // 60)} min. ***")
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
            attempts = self._schedule_retry_locked(state)
            state["uncertain"] = True
            state["status"] = f"turn_incomplete:{stop_reason or 'sin_resultado'}"
            state["status_at"] = self.now()
            state["last_error"] = (
                f"turno sin end_turn; reintento automatico {attempts}")
        self._print(f"\n*** Turno incompleto (stopReason={stop_reason}). "
                    f"Reportes retenidos; reintento automatico #{attempts} en "
                    f"{int(self._retry_interval() // 60)} min. ***")

    def _retry_interval(self) -> float:
        return retry_interval_seconds(self.policy)

    def _schedule_retry_locked(self, state) -> int:
        state["retry_attempts"] = int(state.get("retry_attempts") or 0) + 1
        state["retry_after"] = self.now() + self._retry_interval()
        return int(state["retry_attempts"])

    def _clear_retry_locked(self, state) -> None:
        state["retry_attempts"] = 0
        state["retry_after"] = 0.0
        state["escalated_at"] = 0.0
        state["escalation_count"] = 0

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
                self._recycled_since_turn = False
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
        if not events:
            self._print("El lote incierto ya no esta en la cola (lo desalojo "
                        "el ruido); se libera y sigo con los avisos nuevos.")
            self._ui_event("retry_events_missing",
                           event_ids=list(inflight.get("event_ids") or []))
        with self.store.transaction() as st:
            st["inflight"] = None
            st["uncertain"] = False
            self._clear_retry_locked(st)
        self._send_batch(events, use_initial=bool(inflight.get("use_initial")))

    def _maybe_auto_retry(self) -> bool:
        """Reintenta el lote incierto cuando vence `retry_after` (10 min).

        Devuelve True si disparo un reintento. La entrega sigue siendo
        incierta: se reconecta con `session/load` de la misma sesion para no
        perder el contexto antes de reenviar el mismo lote.
        """
        try:
            state = self.store.load()
        except Exception:
            return False
        inflight = state.get("inflight")
        if not state.get("uncertain") or not inflight:
            return False
        if self.now() < float(state.get("retry_after") or 0.0):
            return False
        if self._transport is None or self._transport.closed:
            self._print("\nReconectando con Devin (misma sesion guardada) "
                        "para el reintento automatico...")
            if not self._connect():
                self._print("No se pudo reconectar; se reintenta en el "
                            "proximo ciclo.")
                return False
        attempt = int(state.get("retry_attempts") or 0)
        self._print(f"\n*** Reintento automatico #{attempt} del lote incierto "
                    f"({len(inflight.get('event_ids') or [])} aviso(s)). ***")
        self._ui_event("auto_retry", attempt=attempt,
                       event_ids=list(inflight.get("event_ids") or []))
        self._retry_inflight()
        return True

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

    def _mark_clean_exit(self, retry_after: float = 0.0) -> None:
        """Sale sin marcar pausa: el watcher reabre cuando haya trabajo."""
        try:
            with self.store.transaction() as st:
                st["worker_ready"] = False
                st["spawn_counted"] = True
                st["last_spawn"] = 0.0
                if retry_after > 0:
                    st["next_retry_after"] = max(
                        float(st.get("next_retry_after") or 0.0),
                        self.now() + retry_after)
        except Exception:
            pass

    def _has_inflight_work(self) -> bool:
        try:
            st = self.store.load()
        except Exception:
            return True
        return bool(st["uncertain"] or st["inflight"])

    def _has_open_work(self) -> bool:
        try:
            st = self.store.load()
        except Exception:
            return True
        if st["uncertain"] or st["inflight"]:
            return True
        return any(e["received_day"] == self.day for e in st["pending"])

    def _main_loop(self) -> int:
        self._start_dispatchers()
        self._heal_truncated_turn()
        last_activity = time.time()
        try:
            while not self._stop.is_set():
                if self._eof_defer and not self._has_sendable():
                    break
                if self.today() != self.day and not self._has_day_work():
                    self._print("Cambio de dia: esta sesion pertenece a "
                                f"{self.day}. Cerrando sin interrumpir turnos.")
                    self._mark_clean_exit()
                    break
                if self._transport and self._transport.closed:
                    self._handle_disconnect()
                    self._drain_backlog()
                    if self._stop.is_set():
                        break
                    if not self._has_inflight_work():
                        # El agente murio entre turnos (p. ej. la guardia de
                        # memoria): salir limpio para que el watcher abra una
                        # consola nueva y retome la sesion guardada.
                        self._print("\nLa conexion con Devin se cerro sin turno "
                                    "en curso; cierro esta consola y el "
                                    "vigilante la reabre para retomar la "
                                    "sesion.")
                        self._ui_event("agent_lost", phase="idle")
                        self._mark_clean_exit(DISCONNECT_RETRY_SECONDS)
                        break
                    time.sleep(self.poll_state_seconds)
                    continue
                if not self._turn_active:
                    if self._should_recycle_agent():
                        if not self._recycle_agent():
                            time.sleep(self.poll_state_seconds)
                            continue
                    self._drain_backlog()
                    if self._stop.is_set():
                        break
                    if self._maybe_auto_retry():
                        last_activity = time.time()
                        continue
                    events = self._take_pending_events()
                    if events:
                        need_rotate = self._should_rotate_session()
                        if not need_rotate and not self._rotation_disabled \
                                and digest_fresh_session(self.policy) \
                                and any(e["level"] in digest_levels(self.policy)
                                        for e in events):
                            try:
                                sess = self.store.load()["sessions"].get(
                                    self.day, {})
                            except Exception:
                                sess = {}
                            need_rotate = bool(
                                sess.get("initial_prompt_sent"))
                        if need_rotate and not self._rotate_session():
                            # No bloquear el lote ni crear sesiones en bucle:
                            # se sigue con la sesion actual y no se reintenta
                            # la rotacion hasta el proximo worker.
                            self._rotation_disabled = True
                        try:
                            sess = self.store.load()["sessions"].get(
                                self.day, {})
                            use_initial = not bool(
                                sess.get("initial_prompt_sent"))
                        except Exception:
                            use_initial = True
                        self._send_batch(events, use_initial=use_initial)
                        last_activity = time.time()
                        continue
                idle = self._idle_close_seconds()
                if idle > 0 and time.time() - last_activity >= idle:
                    if not self._has_open_work():
                        self._print(f"\nSin reportes nuevos hace "
                                    f"{int(idle // 60)} min; cierro esta consola. "
                                    "Se reabre sola cuando Sofia registre un aviso "
                                    "nuevo.")
                        self._ui_event("idle_close")
                        self._mark_clean_exit()
                        break
                    if (not self._turn_active and self._has_inflight_work()
                            and self._transport is not None
                            and not self._transport.closed):
                        self._print(f"\nLote incierto sin actividad hace "
                                    f"{int(idle // 60)} min; cierro el proceso "
                                    "de Devin en segundo plano para liberar "
                                    "memoria (se reconecta con /reintentar).")
                        self._ui_event("idle_agent_parked")
                        self._close_transport()
                        self._disconnect_notified = True
                time.sleep(self.poll_state_seconds)
            if self._stop.is_set():
                try:
                    with self.store.transaction() as st:
                        if st["pending"] or st["uncertain"]:
                            st["paused"] = True
                            st["pause_cursor"] = int(st["cursor"])
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
    err_sink = _worker_stderr_sink(policy, args.group)
    if err_sink is not None:
        sys.stderr = err_sink
    try:
        worker = ConsoleWorker(policy, args.group, day, store)
        return worker.run()
    finally:
        if err_sink is not None:
            try:
                err_sink.flush()
            except Exception:
                pass


def _exit_log(message: str) -> None:
    """Deja rastro de una salida anomala aunque el proceso corra con pythonw.

    pythonw no tiene stdout/stderr: un `return 4` (mutex ocupado), un fallo de
    policy o un traceback se perdian sin dejar evidencia. Se escribe en
    %TEMP%\\sofia_devin_bridge-exit.log (siempre escribible para el usuario).
    """
    text = (f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()} "
            f"argv={' '.join(sys.argv[1:])} {message}\n")
    try:
        path = Path(tempfile.gettempdir()) / "sofia_devin_bridge-exit.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        _rc = main()
    except SystemExit:
        raise
    except BaseException:
        _exit_log("CRASH\n" + tb_mod.format_exc()[-4000:])
        raise
    if _rc:
        _exit_log(f"exit rc={_rc}")
    sys.exit(_rc)
