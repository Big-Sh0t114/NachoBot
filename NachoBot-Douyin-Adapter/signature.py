from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping


SIGNED_HEADERS = ("x-msg-type", "x-nonce-str", "x-roomid", "x-timestamp")


def build_callback_signature(headers: Mapping[str, str], body: bytes, secret: str) -> str:
    """Implement the official live-data callback MD5 + Base64 signature."""
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    pairs = [f"{key}={normalized.get(key, '')}" for key in sorted(SIGNED_HEADERS)]
    raw = "&".join(pairs).encode("utf-8") + body + secret.encode("utf-8")
    return base64.b64encode(hashlib.md5(raw).digest()).decode("ascii")  # noqa: S324


def verify_callback_signature(
    headers: Mapping[str, str],
    body: bytes,
    secret: str,
    *,
    tolerance_seconds: int,
    now: float | None = None,
) -> tuple[bool, str]:
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    signature = normalized.get("x-signature", "").strip()
    if not signature:
        return False, "missing signature"
    if not secret:
        return False, "callback secret is not configured"
    if any(not normalized.get(key) for key in SIGNED_HEADERS):
        return False, "missing signed header"

    try:
        timestamp = float(normalized["x-timestamp"])
    except ValueError:
        return False, "invalid timestamp"
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False, "timestamp outside tolerance"

    expected = build_callback_signature(normalized, body, secret)
    if not hmac.compare_digest(signature, expected):
        return False, "signature mismatch"
    return True, "ok"


def build_webhook_signature(body: bytes, webhook_secret: str) -> str:
    """Implement the official Webhook SHA1(secret + raw body) signature."""
    raw = webhook_secret.encode("utf-8") + body
    return hashlib.sha1(raw).hexdigest()  # noqa: S324


def verify_webhook_signature(body: bytes, webhook_secret: str, signature: str) -> bool:
    if not webhook_secret or not signature:
        return False
    expected = build_webhook_signature(body, webhook_secret)
    return hmac.compare_digest(signature.strip().lower(), expected)
