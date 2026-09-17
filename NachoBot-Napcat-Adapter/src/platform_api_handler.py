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
_RESPONSE_TYPE = "platform_api_response"


def _local_platform() -> str:
    return str(global_config.nachobot_server.platform_name or "").strip()


def _request_content(raw_data: Any) -> dict[str, Any] | None:
    if not isinstance(raw_data, dict):
        return None
    content = raw_data.get("content")
    return content if isinstance(content, dict) else None


def _validate_request(raw_data: Any) -> tuple[str, str] | None:
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
    if content.get("operation") != _COOKIE_OPERATION:
        return None
    if content.get("platform") != _local_platform():
        return None
    params = content.get("params")
    if not isinstance(params, dict) or set(params) != {"domain"}:
        return None
    domain = params.get("domain")
    if not isinstance(domain, str):
        return None
    domain = domain.strip().lower()
    if not domain or len(domain) > 253 or not _DOMAIN_RE.fullmatch(domain):
        return None
    return request_id, domain


async def _send_response(
    request_id: str,
    status: str,
    *,
    cookies: str | None = None,
) -> None:
    response: Dict[str, Any] = {
        "version": _PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": _COOKIE_OPERATION,
        "platform": _local_platform(),
        "status": status,
    }
    if status == "ok" and cookies is not None:
        response["data"] = {"cookies": cookies}
    try:
        await message_send_instance.send_custom_message(
            custom_message=response,
            platform=_local_platform(),
            message_type=_RESPONSE_TYPE,
        )
    except Exception:
        # Do not include request parameters or upstream data in adapter logs.
        logger.error("发送平台能力响应失败")


async def handle_platform_api_request(raw_data: Dict[str, Any]) -> None:
    """Handle one strictly validated typed request from the core."""

    validated = _validate_request(raw_data)
    if validated is None:
        return
    request_id, domain = validated

    try:
        upstream = await nc_message_sender.send_message_to_napcat(
            "get_cookies",
            {"domain": domain},
        )
        if not isinstance(upstream, dict) or upstream.get("status") != "ok":
            await _send_response(request_id, "error")
            return
        data = upstream.get("data")
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if not isinstance(cookies, str):
            await _send_response(request_id, "error")
            return
        await _send_response(request_id, "ok", cookies=cookies)
    except Exception:
        # Keep the response generic and credential-safe.  The core decides how
        # to surface a timeout or unavailable capability to its caller.
        await _send_response(request_id, "error")


__all__ = ["handle_platform_api_request"]
