"""Typed APIs for platform capabilities owned by the core.

Plugins intentionally do not know how a platform adapter is reached.  This
module is the small, typed surface used for capabilities that cannot be
represented as a normal message.  Every operation is explicitly allowlisted;
this is not a generic adapter RPC escape hatch.
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
LIKE_QZONE_OPERATION = "like_qzone"
COMMENT_QZONE_OPERATION = "comment_qzone"
_PROTOCOL_VERSION = 1
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,256}$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_QZONE_TID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_QQ_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_MAX_QZONE_COMMENT_LENGTH = 3000


@dataclass(frozen=True)
class _PendingPlatformRequest:
    platform: str
    operation: str
    future: asyncio.Future


_pending_requests: Dict[str, _PendingPlatformRequest] = {}


class PlatformAPIError(RuntimeError):
    """Raised when an adapter cannot satisfy a typed platform request."""

    def __init__(self, message: str, *, code: str = "upstream_error") -> None:
        super().__init__(message)
        self.code = code


class PlatformAPINotSupportedError(PlatformAPIError):
    """Raised when the selected adapter explicitly lacks a capability."""

    def __init__(self, operation: str) -> None:
        super().__init__(
            f"platform adapter does not support {operation}",
            code="unsupported_operation",
        )


def _validate_domain(domain: str) -> str:
    if not isinstance(domain, str):
        raise TypeError("domain must be a string")
    normalized = domain.strip().lower()
    if not normalized or len(normalized) > 253 or not _DOMAIN_RE.fullmatch(normalized):
        raise ValueError("invalid platform cookie domain")
    return normalized


def _validate_qzone_tid(tid: str) -> str:
    if not isinstance(tid, str):
        raise TypeError("tid must be a string")
    normalized = tid.strip()
    if not _QZONE_TID_RE.fullmatch(normalized):
        raise ValueError("invalid Qzone tid")
    return normalized


def _validate_qzone_uin(target_uin: str | int) -> int:
    if isinstance(target_uin, bool):
        raise TypeError("target_uin must be a QQ number")
    normalized = str(target_uin).strip()
    if not _QQ_RE.fullmatch(normalized):
        raise ValueError("invalid target_uin")
    return int(normalized)


def _validate_abstime(abstime: int) -> int:
    if isinstance(abstime, bool) or not isinstance(abstime, int):
        raise TypeError("abstime must be an integer")
    if abstime < 0 or abstime > 2**63 - 1:
        raise ValueError("invalid abstime")
    return abstime


def _validate_comment_content(content: str) -> str:
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    normalized = content.strip()
    if not normalized or len(normalized) > _MAX_QZONE_COMMENT_LENGTH:
        raise ValueError("invalid Qzone comment content")
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
    if envelope.get("operation") != pending.operation:
        _fail_pending_request(request_id, pending, "unexpected platform API operation")
        return

    try:
        status = envelope.get("status")
        data = envelope.get("data")
        if status == "error":
            error = envelope.get("error")
            error_code = error.get("code") if isinstance(error, dict) else None
            if error_code == "unsupported_operation":
                raise PlatformAPINotSupportedError(pending.operation)
            if not isinstance(error_code, str) or not error_code:
                error_code = "upstream_error"
            raise PlatformAPIError("platform adapter request failed", code=error_code)
        if status != "ok" or not isinstance(data, dict):
            raise PlatformAPIError("adapter returned a malformed platform API response")
        if not pending.future.done():
            pending.future.set_result(data)
    except Exception as exc:
        _pending_requests.pop(request_id, None)
        if not pending.future.done():
            pending.future.set_exception(exc if isinstance(exc, Exception) else PlatformAPIError("invalid response"))


async def _call_platform_operation(
    operation: str,
    params: dict[str, Any],
    *,
    platform: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
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
    _pending_requests[request_id] = _PendingPlatformRequest(expected_platform, operation, future)

    request = {
        "version": _PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": operation,
        "platform": expected_platform,
        "params": params,
    }
    try:
        sent = await get_global_api().send_custom_message(
            expected_platform,
            PLATFORM_API_REQUEST_TYPE,
            request,
        )
        if not sent:
            raise PlatformAPIError("platform API request could not be sent")
        return await asyncio.wait_for(future, timeout_value)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise PlatformAPIError("platform API request timed out") from exc
    finally:
        _pending_requests.pop(request_id, None)


async def get_platform_cookies(
    domain: str,
    *,
    platform: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, str]:
    """Get cookies for ``domain`` through the selected platform adapter.

    Cookie values are never written to logs by this module.
    """

    normalized_domain = _validate_domain(domain)
    data = await _call_platform_operation(
        GET_PLATFORM_COOKIES_OPERATION,
        {"domain": normalized_domain},
        platform=platform,
        timeout=timeout,
    )
    cookie_string = data.get("cookies")
    if not isinstance(cookie_string, str):
        raise PlatformAPIError("adapter returned malformed cookies")
    return _parse_cookie_string(cookie_string)


async def like_qzone(
    tid: str,
    target_uin: str | int,
    *,
    abstime: int = 0,
    platform: Optional[str] = None,
    timeout: float = 30.0,
) -> bool:
    """Like a Qzone feed through an adapter-native implementation."""

    data = await _call_platform_operation(
        LIKE_QZONE_OPERATION,
        {
            "tid": _validate_qzone_tid(tid),
            "target_uin": _validate_qzone_uin(target_uin),
            "abstime": _validate_abstime(abstime),
        },
        platform=platform,
        timeout=timeout,
    )
    if data.get("success") is not True:
        raise PlatformAPIError("adapter returned malformed Qzone like response")
    return True


async def comment_qzone(
    tid: str,
    target_uin: str | int,
    content: str,
    *,
    platform: Optional[str] = None,
    timeout: float = 30.0,
) -> bool:
    """Comment on a Qzone feed through an adapter-native implementation."""

    data = await _call_platform_operation(
        COMMENT_QZONE_OPERATION,
        {
            "tid": _validate_qzone_tid(tid),
            "target_uin": _validate_qzone_uin(target_uin),
            "content": _validate_comment_content(content),
        },
        platform=platform,
        timeout=timeout,
    )
    if data.get("success") is not True:
        raise PlatformAPIError("adapter returned malformed Qzone comment response")
    return True


def pending_request_count() -> int:
    """Return the number of requests awaiting a response (for diagnostics/tests)."""

    return len(_pending_requests)


__all__ = [
    "COMMENT_QZONE_OPERATION",
    "GET_PLATFORM_COOKIES_OPERATION",
    "LIKE_QZONE_OPERATION",
    "PLATFORM_API_REQUEST_TYPE",
    "PLATFORM_API_RESPONSE_TYPE",
    "PlatformAPIError",
    "PlatformAPINotSupportedError",
    "comment_qzone",
    "get_platform_cookies",
    "handle_platform_api_response",
    "like_qzone",
    "pending_request_count",
]
