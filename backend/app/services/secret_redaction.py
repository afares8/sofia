"""Bounded, recursive credential redaction for untrusted evidence and diff gates.

No evaluation of input code. JSON is decoded; Python-style quoted keys/values
use a lexical fallback and literal_eval for string/container literals only.
"""
from __future__ import annotations

import ast
import json
import re

MASK = "[CREDENTIAL]"
MAX_TEXT = 64000
KEY_VALUE = re.compile(r'''(?P<key>"(?:\\.|[^"\\]){1,120}"|'(?:\\.|[^'\\]){1,120}'|[A-Za-z_][A-Za-z0-9_.-]{0,119})\s*[:=]\s*''')
ALIASES = {"pass", "password", "passwd", "pwd", "dbpass", "secret", "secrets", "key", "apikey", "apikeys",
           "token", "tokens", "authorization", "proxyauthorization", "cookie", "setcookie", "credential",
           "credentials", "connectionstring", "dsn", "privatekey", "consumerkey", "accesskey", "awsaccesskeyid"}
SUFFIXES = ("password", "passwd", "apikey", "privatekey", "secret", "token", "credentials", "secretaccesskey")


def sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return normalized in ALIASES or normalized.endswith(SUFFIXES)


def decoded_key(raw: str) -> str:
    if raw[:1] in {'"', "'"}:
        try:
            value = ast.literal_eval(raw)
            return value if isinstance(value, str) else ""
        except (ValueError, SyntaxError, RecursionError):
            return ""
    return raw


def value_end(text: str, start: int) -> int:
    """Find one value without interpreting code; unterminated secrets consume rest."""
    if start >= len(text):
        return start
    if text[start] in {'"', "'", "`"}:
        quote = text[start]
        delimiter = quote*3 if text.startswith(quote*3, start) else quote
        pos = start+len(delimiter)
        while pos < len(text):
            if text[pos] == "\\":
                pos += 2
            elif text.startswith(delimiter, pos):
                return pos+len(delimiter)
            else:
                pos += 1
        return len(text)
    if text[start] in "[{(":
        stack = []
        pos = start
        pairs = {"[": "]", "{": "}", "(": ")"}
        while pos < len(text):
            ch = text[pos]
            if ch in {'"', "'"}:
                pos = value_end(text, pos)
                continue
            if ch in pairs:
                stack.append(pairs[ch])
            elif stack and ch == stack[-1]:
                stack.pop()
                if not stack:
                    return pos+1
            pos += 1
        return len(text)
    auth = re.match(r"(?i)(?:Bearer|Basic)\s+\S+", text[start:])
    if auth:
        return start+auth.end()
    match = re.search(r"[\s,;}\])]+", text[start:])
    return start+match.start() if match else len(text)


def assignments(text: str):
    consumed = 0
    for match in KEY_VALUE.finditer(text):
        if match.start() < consumed or not sensitive_key(decoded_key(match["key"])):
            continue
        end = value_end(text, match.end())
        consumed = end
        yield match.end(), end


def _walk(value, depth: int):
    if depth > 12:
        return "[REDACTED_DEPTH_LIMIT]"
    if isinstance(value, dict):
        return {key: MASK if sensitive_key(str(key)) and item is not None else _walk(item, depth+1)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_walk(item, depth+1) for item in value]
    if isinstance(value, str):
        return _text(value, depth+1)
    return value if value is None or isinstance(value, (bool, int, float)) else "[UNSUPPORTED_VALUE]"


def _text(value: str, depth: int) -> str:
    if depth > 12:
        return "[REDACTED_DEPTH_LIMIT]"
    truncated = len(value) > MAX_TEXT
    value = value[:MAX_TEXT]
    # Decode complete or embedded JSON once per outer fragment; nested keys and
    # escaped/camelCase aliases are then visited structurally, not by regex.
    decoder = json.JSONDecoder()
    pieces = []
    cursor = 0
    plain_start = 0
    attempted = 0
    while attempted < 64:
        found = re.search(r"[\[{]", value[cursor:])
        if not found:
            break
        start = cursor+found.start()
        attempted += 1
        try:
            original, end = decoder.raw_decode(value, start)
        except (ValueError, RecursionError):
            # Literal Python dict/list evidence uses the same recursive policy.
            end = value_end(value, start)
            try:
                original = ast.literal_eval(value[start:end])
                if not isinstance(original, (dict, list, tuple)):
                    raise ValueError()
            except (ValueError, SyntaxError, RecursionError):
                cursor = start+1
                continue
        safe = _walk(original, depth+1)
        pieces.append(_plain(value[plain_start:start]))
        pieces.append(value[start:end] if safe == original else json.dumps(safe, ensure_ascii=False))
        cursor = end
        plain_start = end
    pieces.append(_plain(value[plain_start:]))
    return "".join(pieces)+("\n[TRUNCATED]" if truncated else "")


def _plain(value: str) -> str:
    pieces = []
    cursor = 0
    for start, end in assignments(value):
        pieces.extend((value[cursor:start], json.dumps(MASK)))
        cursor = end
    pieces.append(value[cursor:])
    value = "".join(pieces)
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)", "[PRIVATE KEY]", value, flags=re.S)
    value = re.sub(r"(?i)\b(?:Bearer|Basic)\s+\S+", MASK, value)
    value = re.sub(r"\b(?:github_pat_|gh[pousr]_|sk-|xox[baprs]-)[A-Za-z0-9_-]{10,}", MASK, value)
    value = re.sub(r"\b(?:AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", MASK, value)
    value = re.sub(r"((?:https?|rtsp)://)[^\s/@]+:[^\s/@]+@", r"\1[USERINFO]@", value)
    value = re.sub(r"((?:https?|rtsp)://[^\s?#]+)[?#][^\s]*", r"\1[QUERY]", value)
    value = re.sub(r"(?i)(/(?:token|secret|apikey|api-key|auth)/)[^/\s]+", r"\1[REDACTED]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", value)
    value = re.sub(r"(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)", "[PHONE]", value)
    return value


def redact(value: str) -> str:
    return _text(value, 0)


def contains_literal_credentials(text: str) -> bool:
    """Diff gate: same key aliases, including JSON/Python dicts and nested values.

    Runtime references such as password=os.getenv(...) are not credential
    literals. Nonempty literal strings/containers under a secret key are blocked.
    """
    return _contains_text(text, 0)


def _contains_value(value, depth: int) -> bool:
    if depth > 12:
        return True  # Fail closed for evidence the bounded scanner cannot inspect.
    if isinstance(value, dict):
        return any((sensitive_key(str(key)) and bool(item) and item not in (MASK, "[REDACTED]", "***")) or
                   _contains_value(item, depth+1) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_value(item, depth+1) for item in value)
    return _contains_text(value, depth+1) if isinstance(value, str) else False


def _contains_text(text: str, depth: int) -> bool:
    if depth > 12 or len(text) > MAX_TEXT:
        return True
    # Inspect generic assignment values too: a serialized JSON credential inside
    # a non-secret field must not bypass detection merely by adding escapes.
    for match in KEY_VALUE.finditer(text):
        start = match.end()
        prefix = re.match(r"(?i)(?:br|rb|r|b|f)(?=['\"])", text[start:])
        quote_start = start+(prefix.end() if prefix else 0)
        if text[quote_start:quote_start+1] not in {'"', "'", "`", "{", "[", "("}:
            continue
        end = value_end(text, quote_start)
        literal = text[start:end]
        try:
            value = ast.literal_eval(literal)
        except (ValueError, SyntaxError, RecursionError):
            try:
                value = json.loads(literal)
            except (ValueError, RecursionError):
                if sensitive_key(decoded_key(match["key"])):
                    return True
                continue
        if sensitive_key(decoded_key(match["key"])) and value and value not in (MASK, "[REDACTED]", "***"):
            return True
        if _contains_value(value, depth+1):
            return True
    return False
