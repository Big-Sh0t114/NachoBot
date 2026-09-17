"""Secret-safe, bounded values for SnowLuma adapter diagnostics.

The adapter receives untrusted OneBot payloads and may also handle media refs,
query strings, and provider error messages.  Logging helpers in this module are
deliberately independent of the logger so they can be used by both the client
and bridge without creating a logging cycle.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_REDACTED = "<redacted>"
_TRUNCATED = "<truncated>"
_SENSITIVE_PARTS = {
    "token",
    "accesstoken",
    "authorization",
    "cookie",
    "cookies",
    "secret",
    "password",
    "passwd",
    "credential",
    "credentials",
}
_SENSITIVE_COMPOUND_KEYS = {
    "apikey",
    "appkey",
    "authkey",
    "rkey",
    "sig",
    "signature",
}
_KEY_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_CREDENTIAL_KEY_PATTERN = (
    r"(?:access[_-]?token|authorization|cookie(?:s)?|token|secret|password|"
    r"passwd|credential(?:s)?|api[_-]?key|app[_-]?key|auth[_-]?key|"
    r"rkey|sig(?:nature)?)(?:[_-][a-z0-9]+)*"
)
_QUERY_SECRET_RE = re.compile(
    rf"(?i)(?P<prefix>(?<![a-z0-9])[\"']?{_CREDENTIAL_KEY_PATTERN}[\"']?"
    r"\s*[:=]\s*[\"']?)(?P<value>[^\"'\s,;&}]+)"
)
_BEARER_RE = re.compile(r"(?i)(\b(?:bearer|basic)\s+)[^\s,]+")
_DATA_URL_RE = re.compile(r"(?is)\bdata:[^\s,;]+;base64,[a-z0-9+/=_-]+")


def is_sensitive_key(key: object) -> bool:
    """Return whether a mapping key names a credential-bearing value."""

    raw_key = str(key).casefold()
    normalized = re.sub(r"[^a-z0-9]", "", raw_key)
    if any(part in normalized for part in _SENSITIVE_PARTS):
        return True

    # Match compound spellings such as api_key, api-key, and x.api.key by
    # joining token runs.  Short names like sig/rkey are only accepted as a
    # complete token (or token component), so unrelated words such as assign
    # are not classified as credentials.
    tokens = _KEY_TOKEN_RE.findall(raw_key)
    for start in range(len(tokens)):
        candidate = ""
        for end in range(start, len(tokens)):
            candidate += tokens[end].casefold()
            if candidate in _SENSITIVE_COMPOUND_KEYS:
                return True
    return False


def _scrub_text(text: str, *, max_length: int = 240) -> str:
    text = _DATA_URL_RE.sub("<data-url>", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    text = _QUERY_SECRET_RE.sub(r"\g<prefix>" + _REDACTED, text)
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}...<{len(text) - max_length} chars omitted>"


def sanitize_text(value: object, *, max_length: int = 240) -> str:
    """Scrub credentials and bound an arbitrary value for an exception/log."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<bytes len={len(value)}>"
    return _scrub_text(str(value), max_length=max_length)


def safe_endpoint(url: str) -> str:
    """Display a URL without query credentials, fragments, or userinfo."""

    try:
        parsed = urlsplit(str(url))
    except ValueError:
        return sanitize_text(url, max_length=160)
    netloc = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        netloc = f"{netloc}:{port}"
    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if is_sensitive_key(key):
            continue
        query_items.append((key, sanitize_text(value, max_length=64)))
    query = urlencode(query_items)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def safe_exception(exc: BaseException, *, max_length: int = 240) -> str:
    """Return a bounded exception class/message with URL credentials scrubbed."""

    return f"{type(exc).__name__}: {sanitize_text(exc, max_length=max_length)}"


def short_echo(echo: object, *, length: int = 8) -> str:
    """Correlate actions without emitting a full request identifier."""

    value = str(echo or "")
    return value[:length] if value else "-"


def _is_base64_like(text: str) -> bool:
    if text.startswith("base64://"):
        return True
    if len(text) < 96 or len(text) % 4:
        return False
    try:
        base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return False
    return True


def sanitize_value(value: Any, *, key: object | None = None, depth: int = 0) -> Any:
    """Recursively redact credentials and bound payload values.

    This is intended for explicitly enabled raw debug output, not for
    reconstructing a payload to send.  Mapping keys remain visible for useful
    protocol diagnostics while values are capped and media bodies are replaced
    by length markers.
    """

    if key is not None and is_sensitive_key(key):
        return _REDACTED
    if depth > 5:
        return "<nested value>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<bytes len={len(value)}>"
    if isinstance(value, str):
        # Raw frames can arrive at the logging boundary as JSON text rather
        # than as an already-decoded mapping/list.  Decode only container
        # shaped JSON so ordinary diagnostic strings and malformed JSON keep
        # their existing text-scrubbing behavior.
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (json.JSONDecodeError, RecursionError):
                decoded = None
            if isinstance(decoded, (Mapping, list)):
                return sanitize_value(decoded, key=key, depth=depth)
        if value.startswith("data:") and ";base64," in value:
            return f"<data-url len={len(value)}>"
        if _is_base64_like(value):
            return f"<base64 len={len(value)}>"
        return _scrub_text(value, max_length=180)
    if isinstance(value, Mapping):
        return {
            str(k): sanitize_value(v, key=k, depth=depth + 1)
            for k, v in list(value.items())[:80]
        } | ({"<items>": f"<truncated count={len(value) - 80}>"} if len(value) > 80 else {})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        values = [sanitize_value(item, depth=depth + 1) for item in list(value)[:80]]
        if len(value) > 80:
            values.append(f"<truncated count={len(value) - 80}>")
        return values
    return _scrub_text(repr(value), max_length=180)


def sanitize_payload(payload: Any) -> Any:
    """Alias with an explicit payload-oriented name for raw debug callers."""

    return sanitize_value(payload)


def summarize_payload(payload: Any, *, max_length: int = 1800) -> str:
    """Serialize a redacted payload for opt-in TRACE logging."""

    safe = sanitize_value(payload)
    try:
        text = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = repr(safe)
    return _scrub_text(text, max_length=max_length)


def segment_summary(kind: object, data: Any = None) -> str:
    """Describe one segment without logging message/media contents."""

    label = str(kind or "unknown")
    if isinstance(data, bytes):
        size = len(data)
    elif isinstance(data, str):
        size = len(data)
    elif isinstance(data, Mapping):
        size = 0
        for value in data.values():
            if isinstance(value, (bytes, str)):
                size += len(value)
    elif isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        size = len(data)
    else:
        size = 0
    return f"kind={label} size={size}"


__all__ = [
    "is_sensitive_key",
    "safe_endpoint",
    "safe_exception",
    "short_echo",
    "sanitize_text",
    "sanitize_value",
    "sanitize_payload",
    "summarize_payload",
    "segment_summary",
]
