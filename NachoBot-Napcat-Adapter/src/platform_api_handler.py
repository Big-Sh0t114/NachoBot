"""Allowlisted platform capability handler for the core transport."""

from __future__ import annotations

import re
from typing import Any, Dict

from src.logger import logger
from src.config import global_config
from src.recv_handler.message_sending import message_send_instance
from src.send_handler.nc_sending import nc_message_sender

_PROTOCOL_VERSION = 1
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,256}$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_COOKIE_OPERATION = "get_platform_cookies"
_LIKE_QZONE_OPERATION = "like_qzone"
_COMMENT_QZONE_OPERATION = "comment_qzone"
_QZONE_TID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_QZONE_OPERATIONS = {_LIKE_QZONE_OPERATION, _COMMENT_QZONE_OPERATION}
_RESPONSE_TYPE = "platform_api_response"


def _local_platform() -> str:
    return str(global_config.nachobot_server.platform_name or "").strip()


def _request_content(raw_data: Any) -> dict[str, Any] | None:
    if not isinstance(raw_data, dict):
        return None
    content = raw_data.get("content")
    return content if isinstance(content, dict) else None


def _valid_qzone_uin(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 10**20 - 1


def _validate_request(raw_data: Any) -> tuple[str, str, dict[str, Any]] | None:
    content = _request_content(raw_data)
    if content is None:
        return None
    if raw_data.get("platform") != _local_platform():
        return None
    if content.get("version") != _PROTOCOL_VERSION:
        return None
    request_id = content.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        return None
    operation = content.get("operation")
    if operation not in {_COOKIE_OPERATION, *_QZONE_OPERATIONS}:
        return None
    if content.get("platform") != _local_platform():
        return None
    params = content.get("params")
    if not isinstance(params, dict):
        return None
    if operation == _COOKIE_OPERATION:
        if set(params) != {"domain"}:
            return None
        domain = params.get("domain")
        if not isinstance(domain, str):
            return None
        domain = domain.strip().lower()
        if not domain or len(domain) > 253 or not _DOMAIN_RE.fullmatch(domain):
            return None
        return request_id, operation, {"domain": domain}

    expected_keys = {"tid", "target_uin", "abstime"} if operation == _LIKE_QZONE_OPERATION else {
        "tid", "target_uin", "content"
    }
    if set(params) != expected_keys:
        return None
    tid = params.get("tid")
    if not isinstance(tid, str) or not _QZONE_TID_RE.fullmatch(tid):
        return None
    if not _valid_qzone_uin(params.get("target_uin")):
        return None
    if operation == _LIKE_QZONE_OPERATION:
        abstime = params.get("abstime")
        if isinstance(abstime, bool) or not isinstance(abstime, int) or not 0 <= abstime <= 2**63 - 1:
            return None
    else:
        comment = params.get("content")
        if not isinstance(comment, str) or not comment.strip() or len(comment) > 3000:
            return None
    return request_id, operation, dict(params)


async def _send_response(
    request_id: str,
    operation: str,
    status: str,
    *,
    cookies: str | None = None,
    error_code: str | None = None,
) -> bool:
    response: Dict[str, Any] = {
        "version": _PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": operation,
        "platform": _local_platform(),
        "status": status,
    }
    if status == "ok" and cookies is not None:
        response["data"] = {"cookies": cookies}
    elif status == "error" and error_code:
        response["error"] = {"code": error_code}
    try:
        sent = await message_send_instance.send_custom_message(
            custom_message=response,
            platform=_local_platform(),
            message_type=_RESPONSE_TYPE,
        )
        if not sent:
            logger.error(
                f"发送平台能力响应失败 operation={operation} request_id={request_id[:8]}"
            )
        return sent
    except Exception:
        # Do not include request parameters or upstream data in adapter logs.
        logger.error("发送平台能力响应失败")
        return False


async def handle_platform_api_request(raw_data: Dict[str, Any]) -> None:
    """Handle one strictly validated typed request from the core."""

    validated = _validate_request(raw_data)
    if validated is None:
        content = _request_content(raw_data)
        request_id = content.get("request_id") if content else None
        operation = content.get("operation") if content else None
        logger.warning(
            "平台能力请求校验失败 "
            f"operation={operation if isinstance(operation, str) else 'invalid'} "
            f"request_id={request_id[:8] if isinstance(request_id, str) else 'invalid'}"
        )
        if (
            isinstance(raw_data, dict)
            and raw_data.get("platform") == _local_platform()
            and isinstance(request_id, str)
            and _REQUEST_ID_RE.fullmatch(request_id)
            and operation in {_COOKIE_OPERATION, *_QZONE_OPERATIONS}
        ):
            await _send_response(
                request_id, operation, "error", error_code="invalid_request"
            )
        return
    request_id, operation, params = validated
    logger.debug(
        f"收到平台能力请求 operation={operation} request_id={request_id[:8]}"
    )

    if operation in _QZONE_OPERATIONS:
        # NapCat does not expose a stable native Qzone write action.  Reply
        # immediately so the plugin can use its cookie-backed, typed fallback
        # without waiting for the core request timeout.
        await _send_response(
            request_id,
            operation,
            "error",
            error_code="unsupported_operation",
        )
        return

    try:
        upstream = await nc_message_sender.send_message_to_napcat(
            "get_cookies",
            params,
        )
        if not isinstance(upstream, dict) or upstream.get("status") != "ok":
            await _send_response(request_id, operation, "error", error_code="upstream_error")
            return
        data = upstream.get("data")
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if not isinstance(cookies, str):
            await _send_response(request_id, operation, "error", error_code="upstream_error")
            return
        await _send_response(request_id, operation, "ok", cookies=cookies)
    except Exception:
        # Keep the response generic and credential-safe.  The core decides how
        # to surface a timeout or unavailable capability to its caller.
        await _send_response(request_id, operation, "error", error_code="upstream_error")


__all__ = ["handle_platform_api_request"]
