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
