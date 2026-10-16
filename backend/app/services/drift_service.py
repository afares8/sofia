"""
Detector de "codigo sin desplegar" (drift).

El bug de Vision (2026-10-16) fue de esta clase: el archivo estaba corregido en
disco desde el 15/10 21:21 pero el servicio corria codigo del 14/10 13:56, asi
que el `ValueError` seguia abortando los jobs de IA. Este chequeo compara la
fecha del codigo mas nuevo de cada backend con el arranque del servicio NSSM:
si el codigo es mas nuevo, hay un cambio sin desplegar (o el cambio es inerte).

Read-only. Cacheado para no recorrer los directorios en cada request.
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

# servicio NSSM -> (app, directorio de codigo)
APPS = {
    "MayorBackend": ("mayor", r"C:\Users\ahmed\apps\mayor\backend"),
    "PackingBackend": ("packing", r"C:\Users\ahmed\apps\Packing\backend"),
    "SofiaBackend": ("sofia", r"C:\Users\ahmed\apps\sofia\backend"),
    "VisionBackend": ("vision", r"C:\Users\ahmed\apps\vision\backend"),
    "WPPConnect": ("wppconnect", r"C:\Users\ahmed\apps\wppconnect-server"),
}
CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", "dist",
             "build", "data", "backups", "logs", ".mypy_cache", ".pytest_cache"}
CACHE_SECONDS = 300.0

_lock = threading.Lock()
_cache: Dict = {"at": 0.0, "report": None}


def _newest_code_mtime(root: Path) -> Optional[float]:
    newest: Optional[float] = None
    if not root.is_dir():
        return None
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name not in SKIP_DIRS:
                        stack.append(entry)
                elif entry.suffix in CODE_SUFFIXES:
                    mtime = entry.stat().st_mtime
                    if newest is None or mtime > newest:
                        newest = mtime
            except OSError:
                continue
    return newest


def _service_starts() -> Dict[str, Optional[float]]:
    names = "','".join(APPS)
    script = (
        f"$names = @('{names}'); "
        "Get-CimInstance Win32_Service | Where-Object { $names -contains $_.Name } | "
        "ForEach-Object { "
        "$p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.ProcessId) "
        "-ErrorAction SilentlyContinue; "
        "if ($p) { $_.Name + '|' + $p.CreationDate.ToString('o') } }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=30)
    except Exception:
        return {name: None for name in APPS}
    out: Dict[str, Optional[float]] = {}
    for line in (proc.stdout or "").splitlines():
        if "|" not in line:
            continue
        name, _, raw = line.partition("|")
        name = name.strip()
        raw = raw.strip()
        ts: Optional[float] = None
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(raw).timestamp()
        except Exception:
            ts = None
        out[name] = ts
    for name in APPS:
        out.setdefault(name, None)
    return out


def check(force: bool = False) -> Dict:
    """Reporte de drift por app. Cacheado CACHE_SECONDS."""
    with _lock:
        if not force and _cache["report"] is not None \
                and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["report"]
    starts = _service_starts()
    items: List[Dict] = []
    for service_name, (app, code_dir) in APPS.items():
        newest = _newest_code_mtime(Path(code_dir))
        started = starts.get(service_name)
        drift = None
        if newest is not None and started is not None:
            drift = newest > started
        items.append({
            "app": app,
            "service": service_name,
            "service_started": started,
            "newest_code": newest,
            "drift": drift,
        })
    report = {
        "checked_at": time.time(),
        "drifted": sorted(i["app"] for i in items if i["drift"]),
        "items": items,
    }
    with _lock:
        _cache["at"] = time.time()
        _cache["report"] = report
    return report


if __name__ == "__main__":  # pragma: no cover - utilidad manual
    print(json.dumps(check(force=True), indent=2, default=str))
    sys.exit(0)
