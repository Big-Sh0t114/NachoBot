"""Typed APIs for platform capabilities owned by the core.

Plugins intentionally do not know how a platform adapter is reached.  This
module is the small, typed surface used for capabilities that cannot be
represented as a normal message (currently cookie retrieval).
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.common.message.api import get_global_api
from src.config.config import global_config

PLATFORM_API_REQUEST_TYPE = "platform_api_request"
PLATFORM_API_RESPONSE_TYPE = "platform_api_response"
GET_PLATFORM_COOKIES_OPERATION = "get_platform_cookies"
_PROTOCOL_VERSION = 1
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,256}$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class _PendingPlatformRequest:
    platform: str
    future: asyncio.Future


_pending_requests: Dict[str, _PendingPlatformRequest] = {}


class PlatformAPIError(RuntimeError):
    """Raised when an adapter cannot satisfy a typed platform request."""


def _validate_domain(domain: str) -> str:
    if not isinstance(domain, str):
        raise TypeError("domain must be a string")
    normalized = domain.strip().lower()
    if not normalized or len(normalized) > 253 or not _DOMAIN_RE.fullmatch(normalized):
        raise ValueError("invalid platform cookie domain")
    return normalized


def _resolve_platform(platform: Optional[str]) -> str:
    if platform is not None:
        if not isinstance(platform, str):
            raise TypeError("platform must be a string")
        normalized = platform.strip()
        if not normalized:
            raise ValueError("platform must not be empty")
        return normalized

    configured = getattr(getattr(global_config, "bot", None), "platform", None)
    if not isinstance(configured, str) or not configured.strip():
        raise PlatformAPIError("platform is not configured")
    return configured.strip()


def _new_request_id() -> str:
    # token_urlsafe is intentionally used instead of a timestamp or a counter;
    # request IDs cross a trust boundary and must not be guessable.
    return secrets.token_urlsafe(32)


def _parse_cookie_string(cookie_string: str) -> dict[str, str]:
    if not isinstance(cookie_string, str):
        raise PlatformAPIError("adapter returned malformed cookies")

    cookies: dict[str, str] = {}
    for raw_pair in cookie_string.split(";"):
        pair = raw_pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        if key:
            cookies[key] = value.strip()
    return cookies


def _extract_response_envelope(raw_data: Any) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(raw_data, dict):
        return None, None
    outer_platform = raw_data.get("platform")
    content = raw_data.get("content")
    if not isinstance(content, dict):
        return outer_platform if isinstance(outer_platform, str) else None, None
    platform = outer_platform if isinstance(outer_platform, str) else content.get("platform")
    return platform if isinstance(platform, str) else None, content


def _fail_pending_request(request_id: str, pending: _PendingPlatformRequest, message: str) -> None:
    """Reject and remove a request that has received a terminal bad response."""

    _pending_requests.pop(request_id, None)
    if not pending.future.done():
        pending.future.set_exception(PlatformAPIError(message))


async def handle_platform_api_response(raw_data: Dict[str, Any]) -> None:
    """Resolve a pending typed request from an adapter response.

    The outer transport platform and the envelope platform are both accepted
    as the binding when present, but a supplied value must agree with the
    pending request.  Unknown or mismatched responses are deliberately left
    unresolved and therefore expire through the caller's normal timeout path.
    """

    response_platform, envelope = _extract_response_envelope(raw_data)
    if envelope is None:
        return

    request_id = envelope.get("request_id")
    if not isinstance(request_id, str):
        return
    pending = _pending_requests.get(request_id)
    if pending is None or response_platform != pending.platform:
        return

    if envelope.get("platform") not in (None, pending.platform):
        return
    # A response without the mandatory version is incomplete and cannot
    # resolve the waiter.  Keep waiting for a valid correlated response;
    # an explicitly supplied unsupported version is terminal for this request.
    if "version" not in envelope:
        return
    if envelope["version"] != _PROTOCOL_VERSION:
        _fail_pending_request(request_id, pending, "unsupported platform API response")
        return
    if envelope.get("operation") != GET_PLATFORM_COOKIES_OPERATION:
        _fail_pending_request(request_id, pending, "unexpected platform API operation")
        return

    try:
        status = envelope.get("status")
        data = envelope.get("data")
        if status != "ok" or not isinstance(data, dict) or not isinstance(data.get("cookies"), str):
            raise PlatformAPIError("adapter returned an invalid cookie response")
        if not pending.future.done():
            pending.future.set_result(data["cookies"])
    except Exception as exc:
        _pending_requests.pop(request_id, None)
        if not pending.future.done():
            pending.future.set_exception(exc if isinstance(exc, Exception) else PlatformAPIError("invalid response"))


async def get_platform_cookies(
    domain: str,
    *,
    platform: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, str]:
    """Get cookies for ``domain`` through the selected platform adapter.

    The adapter receives only this typed operation.  Cookie values are never
    written to logs by this module.
    """

    normalized_domain = _validate_domain(domain)
    expected_platform = _resolve_platform(platform)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a positive number") from exc
    if timeout_value <= 0:
        raise ValueError("timeout must be a positive number")

    request_id = _new_request_id()
    while request_id in _pending_requests:
        request_id = _new_request_id()
    future = asyncio.get_running_loop().create_future()
    _pending_requests[request_id] = _PendingPlatformRequest(expected_platform, future)

    request = {
        "version": _PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": GET_PLATFORM_COOKIES_OPERATION,
        "platform": expected_platform,
        "params": {"domain": normalized_domain},
    }
    try:
        sent = await get_global_api().send_custom_message(
            expected_platform,
            PLATFORM_API_REQUEST_TYPE,
            request,
        )
        if not sent:
            raise PlatformAPIError("platform API request could not be sent")
        cookie_string = await asyncio.wait_for(future, timeout_value)
        return _parse_cookie_string(cookie_string)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise PlatformAPIError("platform API request timed out") from exc
    finally:
        _pending_requests.pop(request_id, None)


def pending_request_count() -> int:
    """Return the number of requests awaiting a response (for diagnostics/tests)."""

    return len(_pending_requests)


__all__ = [
    "GET_PLATFORM_COOKIES_OPERATION",
    "PLATFORM_API_REQUEST_TYPE",
    "PLATFORM_API_RESPONSE_TYPE",
    "PlatformAPIError",
    "get_platform_cookies",
    "handle_platform_api_response",
    "pending_request_count",
]
