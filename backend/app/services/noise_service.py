"""
Supresion de ruido: mensajes conocido-esperados que NO deben crear issues.

Los patrones viven en `config.json` (`log_ignore_patterns`) y se cachean por
mtime. Se aplican en `db_service.upsert_event`, el punto unico de creacion de
issues, asi que cubren tanto la ingesta activa (SDK) como la pasiva (logs).

Usar SOLO para familias con evidencia de que son esperadas; nunca para ocultar
fallos reales. Cada patron declara `id`, `pattern` (regex, case-insensitive,
busqueda parcial), `service_id` opcional y una `note` con la razon.
"""
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

CONFIG_PATH = Path(os.getenv(
    "SOFIA_CONFIG_PATH",
    str(Path(__file__).resolve().parents[2] / "data" / "config.json")))

_cache_lock = threading.Lock()
_cache: Dict = {"mtime": None, "patterns": []}
_skips: Dict[str, int] = {}
_last_skip: Dict[str, float] = {}


def _load_patterns() -> List[Dict]:
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return []
    with _cache_lock:
        if _cache["mtime"] == mtime:
            return _cache["patterns"]
    compiled: List[Dict] = []
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for item in data.get("log_ignore_patterns") or []:
            if not isinstance(item, dict):
                continue
            pattern = str(item.get("pattern") or "")
            if not pattern:
                continue
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error:
                continue
            compiled.append({
                "id": str(item.get("id") or pattern),
                "service_id": str(item["service_id"]) if item.get("service_id") else None,
                "rx": rx,
                "note": str(item.get("note") or ""),
            })
    except (OSError, json.JSONDecodeError):
        compiled = []
    with _cache_lock:
        _cache["mtime"] = mtime
        _cache["patterns"] = compiled
    return compiled


def ignored_reason(service_id: str, message: str) -> Optional[str]:
    """Devuelve el id del patron que marca el mensaje como esperado, o None."""
    if not message:
        return None
    for pat in _load_patterns():
        if pat["service_id"] and pat["service_id"] != service_id:
            continue
        if pat["rx"].search(message):
            return pat["id"]
    return None


def record_skip(service_id: str, pattern_id: str) -> None:
    key = f"{service_id}:{pattern_id}"
    _skips[key] = _skips.get(key, 0) + 1
    _last_skip[key] = time.time()


def get_stats() -> Dict:
    """Contadores de mensajes suprimidos (observabilidad)."""
    return {"counts": dict(_skips), "last_skip_at": dict(_last_skip)}


def reset_stats() -> None:
    _skips.clear()
    _last_skip.clear()
