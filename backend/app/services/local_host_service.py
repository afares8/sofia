"""
Local host monitor - watches SERVER-SOFIA itself.

Everything runs locally as LocalSystem; no SSH needed. Covers the things the
HTTP health checks cannot see:

- Disk usage (C:), memory pressure, sustained CPU saturation (psutil)
- NSSM service state changes (a service Stopped but not yet detected by a
  health-check threshold)
- Windows Event Log Critical/Error entries (unexpected shutdowns, disk/Ntfs
  errors, WHEA hardware faults, unexpected service terminations, app crashes)
- TLS certificate expiry for the public droplet endpoint (elenatec.com)

State (last event RecordIds, latched flags, service states) is persisted to
data/local_host_state.json so restarts do not re-ingest history.

Env knobs (backend/.env):
  SOFIA_LOCAL_MONITOR       true/false             (default true)
  SOFIA_LOCAL_POLL_S        seconds between polls  (default 120)
  SOFIA_LOCAL_DISK_WARN     disk % warning         (default 85)
  SOFIA_LOCAL_DISK_CRIT     disk % critical        (default 93)
  SOFIA_LOCAL_MEM_WARN      free mem % warning     (default 10)
  SOFIA_LOCAL_CPU_WARN      cpu % warning          (default 95, 3 consecutive polls)
  SOFIA_LOCAL_CERT_WARN_D   cert days-left warning (default 14)
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import ssl
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

from app.models.event import ErrorEvent
from app.services import db_service, whatsapp_service
from app.services.config_service import load_config

logger = logging.getLogger("sofia.localhost")

ENABLED = os.getenv("SOFIA_LOCAL_MONITOR", "true").lower() in ("1", "true", "yes")
POLL_SECONDS = int(os.getenv("SOFIA_LOCAL_POLL_S", "120"))
DISK_WARN = int(os.getenv("SOFIA_LOCAL_DISK_WARN", "85"))
DISK_CRIT = int(os.getenv("SOFIA_LOCAL_DISK_CRIT", "93"))
MEM_WARN = int(os.getenv("SOFIA_LOCAL_MEM_WARN", "10"))
CPU_WARN = int(os.getenv("SOFIA_LOCAL_CPU_WARN", "95"))
CPU_SUSTAIN_POLLS = 3
CERT_WARN_DAYS = int(os.getenv("SOFIA_LOCAL_CERT_WARN_D", "14"))
CERT_CHECK_EVERY_S = 3600
CERT_HOSTNAME = os.getenv("SOFIA_LOCAL_CERT_HOST", "elenatec.com")

STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "local_host_state.json"

# Windows services Sofia should expect to be Running.
WATCHED_SERVICES = [
    "MayorBackend", "MayorFrontend",
    "PackingBackend", "PackingFrontend",
    "SofiaFrontend",
    "WPPConnect", "NgrokTunnel", "SofiaTunnel",
    "RustDesk",
    "VisionBackend", "VisionMedia", "vms-api",
]

# Event Log sources whose Error events are always meaningful.
_ERROR_SOURCES = {
    "disk", "ntfs", "volmgr", "whea-logger", "bugcheck",
    "microsoft-windows-kernel-power", "microsoft-windows-wininit",
    "application error", "windows error reporting", ".net runtime",
}
# Event IDs that are notorious noise even at Error level.
_IGNORE_EVENT_IDS = {10016}  # DistributedCOM permission spam
# SCM IDs worth alerting (unexpected termination / crash).
_SCM_ALERT_IDS = {7031, 7034}


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


# ── persisted state ──────────────────────────────────────────────────────────
_last_record_id: Dict[str, int] = {}          # log name -> max RecordId seen
_svc_state: Dict[str, str] = {}               # service -> last known status
_flags: Dict[str, bool] = {}
_seen_ids: Set[str] = set()
_cpu_high_polls = 0
_last_cert_check = 0.0
_status: Dict = {
    "enabled": ENABLED,
    "last_poll": None,
    "last_error": None,
    "last_ingest_count": 0,
    "consecutive_failures": 0,
}


def get_monitor_status() -> Dict:
    return dict(_status)


def _load_state() -> None:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        _last_record_id.update(
            {k: int(v) for k, v in (raw.get("record_ids") or {}).items()})
        _svc_state.update(raw.get("services") or {})
        _flags.update(raw.get("flags") or {})
        _seen_ids.update(raw.get("seen") or [])
        logger.info("[LOCAL] State loaded.")
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"[LOCAL] Could not load state: {exc}")


def _save_state() -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "record_ids": _last_record_id,
            "services": _svc_state,
            "flags": _flags,
            "seen": sorted(_seen_ids)[-2000:],
        }
        with tempfile.NamedTemporaryFile(
            "w", dir=str(STATE_PATH.parent), delete=False,
            encoding="utf-8", suffix=".tmp",
        ) as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name
        os.replace(tmp_path, STATE_PATH)
    except Exception as exc:
        logger.debug(f"[LOCAL] Could not save state: {exc}")


# ── ingestion helper ─────────────────────────────────────────────────────────
async def _store_and_alert(event: ErrorEvent) -> None:
    issue_id, is_new, is_regression = await db_service.upsert_event(event)
    if event.level in ("ERROR", "CRITICAL") and (is_new or is_regression):
        try:
            cfg = load_config()
            prefix = "🔄 REGRESIÓN: " if is_regression else ""
            sent = await whatsapp_service.send_alert(
                cfg.alerts, event.service_name, event.service_id,
                event.level, prefix + event.message[:200])
            if sent:
                await db_service.mark_notified(issue_id)
        except Exception as exc:
            logger.warning(f"[LOCAL] Alert send failed ({type(exc).__name__})")


async def _host_event(level: str, key: str, message: str,
                      detail: str = "") -> bool:
    dedup_key = _sha(f"{key}|{message}")
    if dedup_key in _seen_ids:
        return False
    await _store_and_alert(ErrorEvent(
        service_id="server-sofia",
        service_name="SERVER-SOFIA Host",
        level=level,
        message=message[:500],
        detail=detail or f"local host signal: {key}",
        source="local",
        timestamp=datetime.utcnow(),
    ))
    _seen_ids.add(dedup_key)
    return True


# ── collectors (sync, run via asyncio.to_thread) ─────────────────────────────
def _collect_metrics() -> Dict:
    import psutil
    disk = psutil.disk_usage("C:/")
    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=1)
    return {
        "disk_pct": round(disk.percent, 1),
        "mem_avail_pct": round(mem.available * 100 / mem.total, 1),
        "cpu_pct": round(cpu, 1),
    }


def _collect_service_states() -> Dict[str, str]:
    """service name -> 'Running'/'Stopped'/... via sc.exe (no admin needed)."""
    states: Dict[str, str] = {}
    for name in WATCHED_SERVICES:
        try:
            out = subprocess.run(
                ["sc.exe", "query", name],
                capture_output=True, text=True, timeout=10,
            ).stdout
            m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", out)
            states[name] = m.group(1) if m else "UNKNOWN"
        except Exception:
            states[name] = "UNKNOWN"
    return states


def _collect_events(minutes_back: int = 3) -> List[Dict]:
    """Critical/Error events from System+Application logs since minutes_back."""
    since = (datetime.now() - timedelta(minutes=minutes_back)).strftime(
        "%m/%d/%Y %H:%M:%S")
    queries = []
    for log_name in ("System", "Application"):
        rid = int(_last_record_id.get(log_name) or 0)
        if rid > 0:
            queries.append(
                f"Get-WinEvent -LogName '{log_name}' -FilterXPath "
                f"'*[System[(Level=1 or Level=2) and "
                f"(EventRecordID > {rid})]]' -Oldest -MaxEvents 150 "
                "-ErrorAction SilentlyContinue")
        else:
            queries.append(
                f"Get-WinEvent -FilterHashtable @{{LogName='{log_name}';"
                f"Level=1,2;StartTime='{since}'}} -MaxEvents 150 "
                "-ErrorAction SilentlyContinue")
    cmd = (
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
        f"@({'; '.join(queries)})"
        " | Select-Object RecordId,LogName,LevelDisplayName,ProviderName,Id,"
        "TimeCreated,Message | ConvertTo-Json -Depth 2 -Compress"
    )
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    ).stdout.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]


def _check_cert(hostname: str) -> Optional[int]:
    """Days until TLS cert expiry, or None on failure."""
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(
            __import__("socket").socket(), server_hostname=hostname
        ) as s:
            s.settimeout(10)
            s.connect((hostname, 443))
            cert = s.getpeercert()
        exp = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        return (exp - datetime.utcnow()).days
    except Exception as exc:
        logger.debug(f"[LOCAL] cert check failed for {hostname}: {exc}")
        return None


def _redact_host_detail(text: str) -> str:
    text = str(text)
    text = re.sub(r'(?i)([?&](?:t|token|key|api_key|access_token|secret|password)=)[^\s&#\"\']+', r'\1[REDACTADO]', text)
    text = re.sub(r'(?i)(\b(?:authorization|bearer|password|passwd|secret|api[_-]?key|authtoken|access_token)\b[\"\']?\s*[:=]?\s*[\"\']?)[^\s,}\"\']+', r'\1[REDACTADO]', text)
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[EMAIL]', text)
    text = re.sub(r'(?i)("(?:phone|whatsapp_number|recipient_number)"\s*:\s*")[^"]+', r'\1[REDACTADO]', text)
    text = re.sub(r'(/api/hikvision/events/)[^\s/\"\'?]+', r'\1[REDACTADO]', text)
    text = re.sub(r'(https?://[^\s\"\'<>?]+)\?[^\s\"\'<>]+', r'\1?[REDACTADO]', text)
    text = re.sub(r'(?i)((?:https?|rtsp)://)[^/\s:@]+:[^@\s]+@', r'\1[REDACTADO]@', text)
    text = re.sub(r'\b\d{8,15}\b', '[NUMERO_REDACTADO]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[TOKEN_REDACTADO]', text)
    text = re.sub('[\U0001F300-\U0001FAFF\u2600-\u27BF]', '', text)
    return text


_CAUSAL_MARKERS = ("Exception Info:", "Exception code:", "Faulting module name:")


# ── classification + ingest ──────────────────────────────────────────────────
def _classify_event(ev: Dict) -> Optional[str]:
    """Map a Windows event to WARNING/ERROR/CRITICAL or None (skip)."""
    provider = (ev.get("ProviderName") or "").lower()
    eid = int(ev.get("Id") or 0)
    level = (ev.get("LevelDisplayName") or "").lower()
    if eid in _IGNORE_EVENT_IDS:
        return None
    if eid == 1022 and provider == ".net runtime":
        return "WARNING"
    if "service control manager" in provider:
        return "ERROR" if eid in _SCM_ALERT_IDS else "WARNING"
    if provider in _ERROR_SOURCES or "error" in provider:
        return "CRITICAL" if level == "critical" else "ERROR"
    return "CRITICAL" if level == "critical" else "WARNING"


async def _process_metrics(m: Dict) -> int:
    global _cpu_high_polls
    count = 0

    disk = m["disk_pct"]
    if disk >= DISK_CRIT and not _flags.get("disk_crit"):
        _flags["disk_crit"] = True
        count += await _host_event(
            "CRITICAL", "disk", f"C: drive at {disk}% — critically low")
    elif disk >= DISK_WARN and not _flags.get("disk_warn"):
        _flags["disk_warn"] = True
        count += await _host_event(
            "WARNING", "disk", f"C: drive at {disk}%")
    elif disk < DISK_WARN:
        _flags["disk_warn"] = _flags["disk_crit"] = False

    mem = m["mem_avail_pct"]
    if mem <= MEM_WARN and not _flags.get("mem_low"):
        _flags["mem_low"] = True
        count += await _host_event(
            "WARNING", "mem", f"SERVER-SOFIA memory available {mem}%")
    elif mem > MEM_WARN:
        _flags["mem_low"] = False

    if m["cpu_pct"] >= CPU_WARN:
        _cpu_high_polls += 1
        if _cpu_high_polls >= CPU_SUSTAIN_POLLS and not _flags.get("cpu_high"):
            _flags["cpu_high"] = True
            count += await _host_event(
                "WARNING", "cpu",
                f"CPU sustained at {m['cpu_pct']}% for {_cpu_high_polls} polls")
    else:
        _cpu_high_polls = 0
        _flags["cpu_high"] = False
    return count


async def _process_services(states: Dict[str, str]) -> int:
    count = 0
    for name, state in states.items():
        prev = _svc_state.get(name)
        _svc_state[name] = state
        if state == prev or prev is None:
            continue
        if state not in ("RUNNING", "START_PENDING"):
            level = "WARNING" if state == "PAUSED" else "ERROR"
            count += await _host_event(
                level, f"svc_{name}",
                f"Windows service {name} is {state}")
        elif prev not in ("RUNNING", "START_PENDING"):
            count += await _host_event(
                "WARNING", f"svc_{name}",
                f"Windows service {name} recovered: {prev} -> {state}")
    return count


async def _process_events(events: List[Dict]) -> int:
    count = 0
    for ev in sorted(events, key=lambda e: int(e.get("RecordId") or 0)):
        log_name = ev.get("LogName") or "System"
        rid = int(ev.get("RecordId") or 0)
        if rid and rid <= _last_record_id.get(log_name, 0):
            continue
        level = _classify_event(ev)
        if not level:
            if rid and rid > _last_record_id.get(log_name, 0):
                _last_record_id[log_name] = rid
            continue
        provider = ev.get("ProviderName") or "?"
        eid = ev.get("Id")
        redacted_full = _redact_host_detail((ev.get("Message") or "").strip())
        lines = [ln.strip() for ln in redacted_full.splitlines() if ln.strip()]
        summary = lines[0] if lines else ""
        causal_lines = []
        for ln in lines[1:]:
            if ln not in causal_lines and any(
                    marker in ln for marker in _CAUSAL_MARKERS):
                causal_lines.append(ln)
        if causal_lines:
            summary = f"{summary} | {' | '.join(causal_lines)}"
        detail = json.dumps({
            "record_id": rid,
            "log_name": log_name,
            "event_id": eid,
            "provider": provider,
            "source_time": ev.get("TimeCreated"),
            "message": redacted_full[:12000],
            "message_truncated": len(redacted_full) > 12000,
        }, ensure_ascii=False)
        accepted = await _host_event(
            level, f"ev_{log_name}_{eid}",
            f"Windows {log_name} event {eid} [{provider}]: {summary}",
            detail=detail)
        count += 1 if accepted else 0
        if rid and rid > _last_record_id.get(log_name, 0):
            _last_record_id[log_name] = rid
    return count


async def _process_cert() -> int:
    global _last_cert_check
    import time
    now = time.time()
    if now - _last_cert_check < CERT_CHECK_EVERY_S:
        return 0
    _last_cert_check = now
    days = await asyncio.to_thread(_check_cert, CERT_HOSTNAME)
    if days is None:
        return 0
    if days <= 7:
        return await _host_event(
            "ERROR", "cert", f"{CERT_HOSTNAME} TLS cert expires in {days}d")
    if days <= CERT_WARN_DAYS:
        return await _host_event(
            "WARNING", "cert", f"{CERT_HOSTNAME} TLS cert expires in {days}d")
    return 0


async def _collect_once() -> int:
    count = 0
    metrics, states, events = await asyncio.gather(
        asyncio.to_thread(_collect_metrics),
        asyncio.to_thread(_collect_service_states),
        asyncio.to_thread(_collect_events),
    )
    count += await _process_metrics(metrics)
    count += await _process_services(states)
    count += await _process_events(events)
    count += await _process_cert()
    return count


async def local_host_loop() -> None:
    """Background task: watch the Windows host every POLL_SECONDS."""
    if not ENABLED:
        logger.info("[LOCAL] Local host monitor disabled (SOFIA_LOCAL_MONITOR).")
        return
    try:
        import psutil  # noqa: F401
    except ImportError:
        logger.error("[LOCAL] psutil not installed — local monitor inactive.")
        _status["last_error"] = "psutil not installed"
        return

    _load_state()
    logger.info(f"[LOCAL] Local host monitor started (poll={POLL_SECONDS}s).")

    while True:
        try:
            n = await _collect_once()
            _status["last_poll"] = datetime.utcnow().isoformat()
            _status["last_error"] = None
            _status["last_ingest_count"] = n
            _status["consecutive_failures"] = 0
            if n:
                logger.info(f"[LOCAL] Ingested {n} local host event(s).")
            _save_state()
        except Exception as exc:
            fails = int(_status.get("consecutive_failures") or 0) + 1
            _status["consecutive_failures"] = fails
            _status["last_error"] = str(exc)[:300]
            logger.warning(f"[LOCAL] Poll failed ({fails}x): {exc}")
        await asyncio.sleep(POLL_SECONDS)
