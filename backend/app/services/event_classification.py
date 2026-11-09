def normalize_reported_level(level: str, message: str) -> str:
    normalized = level.upper()
    if normalized not in ("ERROR", "CRITICAL"):
        return normalized
    lowered_message = message.casefold()
    if (
        "transition was aborted because of invalid state" in lowered_message
        or "old view transition aborted by new view transition" in lowered_message
        or (
            "session disconnected" in lowered_message
            and ("wppconnect" in lowered_message or "whatsapp" in lowered_message)
        )
    ):
        return "WARNING"
    return normalized


_EMPTY_MESSAGES = {"", "{}", "[]", "null", "none", "undefined", '""', "''"}
# Marcadores que el SDK antepone en la misma linea ("[traceback] TypeError: ...")
_LINE_MARKERS = ("[detail]", "[traceback]", "[breadcrumbs]",
                 "console.error:", "console.error", "console.warn:", "console.warn")
_SKIP_PREFIXES = ("status:", "duration:", "at ")


def useful_message(message: str, detail: str = "",
                   traceback: str = "") -> str:
    """Mensaje legible cuando el SDK manda un Error serializado como '{}'.

    Caso real 2026-11-09 (issue 82613, 9 ocurrencias): el frontend de Mayor
    reporta `message='{}'` y el error real (`TypeError: Failed to fetch
    dynamically imported module: ...`) queda solo en `detail`, asi que el
    triaje veia un aviso vacio. Se conserva el mensaje original cuando aporta
    y, si no, se toma la primera linea util de detail/traceback.
    """
    raw = (message or "").strip()
    if raw.casefold() not in _EMPTY_MESSAGES:
        return raw
    for source in (detail or "", traceback or ""):
        for line in source.splitlines():
            candidate = line.strip()
            lowered = candidate.casefold()
            stripped = True
            while stripped:
                stripped = False
                for marker in _LINE_MARKERS:
                    if lowered.startswith(marker):
                        candidate = candidate[len(marker):].strip()
                        lowered = candidate.casefold()
                        stripped = True
            if not candidate or lowered in _EMPTY_MESSAGES:
                continue
            if any(lowered.startswith(p) for p in _SKIP_PREFIXES):
                continue
            return candidate[:300]
    return "(sin mensaje)"
