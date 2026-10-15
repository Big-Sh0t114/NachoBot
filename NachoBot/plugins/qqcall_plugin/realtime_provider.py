"""QQCall-owned verification for Core's provider-neutral realtime ingress."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ncnk_message import get_system_event, get_system_event_route
from qqcall_client.qqcall_service import is_qqcall_enabled
from src.chat.runtime_capabilities import additional_config_from_message, runtime_capabilities_from_message
from plugins.qqcall_plugin.control_api import QQCallAPIError, request_qqcall, validate_qqcall_scope
from src.plugin_system.apis.realtime_api import RealtimeLifecycleChange, RealtimeSession

_SCOPE_KEYS = (
    "serviceInstance",
    "hostGeneration",
    "connectionGeneration",
    "callId",
    "callGeneration",
)
_STATUS_TIMEOUT_SECONDS = 2.5
_SESSION_TTL_SECONDS = 120.0


def qqcall_enabled() -> bool:
    """Read the canonical enable switch on every proof and lease operation."""

    try:
        return is_qqcall_enabled() is True
    except Exception:
        return False


def _message_platform(message: Any) -> str:
    info = getattr(message, "message_info", None)
    chat_info = getattr(message, "chat_info", None)
    return str(
        getattr(info, "platform", None)
        or getattr(chat_info, "platform", None)
        or getattr(message, "chat_info_platform", None)
        or ""
    ).strip()


def _message_group_id(message: Any) -> Any:
    info = getattr(message, "message_info", None)
    group_info = getattr(info, "group_info", None) or getattr(message, "group_info", None)
    if group_info is not None:
        return getattr(group_info, "group_id", None)
    return getattr(message, "chat_info_group_id", None)


def _message_user_id(message: Any) -> Any:
    info = getattr(message, "message_info", None)
    user_info = getattr(message, "user_info", None) or getattr(info, "user_info", None)
    return getattr(user_info, "user_id", None) if user_info is not None else None


def _message_id(message: Any) -> str:
    info = getattr(message, "message_info", None)
    return str(getattr(info, "message_id", None) or getattr(message, "message_id", None) or "")


def _realtime_annotation_matches(
    annotation: RealtimeSession,
    scope: dict[str, Any],
    turn_id: Any,
) -> bool:
    expected_session_id = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (
        annotation.version == 1
        and annotation.provider == "qqcall"
        and annotation.session_id == expected_session_id
        and isinstance(turn_id, str)
        and bool(turn_id)
        and annotation.turn_id == turn_id
    )


def _voice_details(
    message: Any,
    chat_id: str,
    annotation: RealtimeSession,
) -> tuple[str, dict[str, Any], str] | None:
    if get_system_event(message) is not None or _message_group_id(message) is not None:
        return None
    config = additional_config_from_message(message)
    try:
        scope = validate_qqcall_scope(config.get("qqcall_scope"))
    except QQCallAPIError:
        return None
    target = config.get("delivery_target")
    platform = _message_platform(message)
    peer_user_id = _message_user_id(message)
    capabilities = runtime_capabilities_from_message(message)
    if (
        not qqcall_enabled()
        or platform != "qq"
        or not isinstance(target, dict)
        or target.get("kind") != "qqcall"
        or any(target.get(key) != scope[key] for key in _SCOPE_KEYS)
        or not isinstance(target.get("peer_user_id"), str)
        or target.get("peer_user_id") != peer_user_id
        or not isinstance(peer_user_id, str)
        or not peer_user_id.isdigit()
        or not _realtime_annotation_matches(annotation, scope, target.get("turn_id"))
        or not _message_id(message).startswith("qqcall:voice:")
        or not _message_id(message).endswith(":" + annotation.turn_id)
        or not capabilities.planner_bypass
        or capabilities.reply_delivery != "tts_text"
        or not capabilities.voice_stream
    ):
        return None
    try:
        from src.chat.message_receive.chat_stream import get_chat_manager

        expected_chat_id = get_chat_manager().get_stream_id(platform, peer_user_id, is_group=False)
    except Exception:
        return None
    if expected_chat_id != chat_id:
        return None
    return peer_user_id, scope, platform


async def _current_call_matches(
    platform: str,
    peer_user_id: str,
    scope: dict[str, Any],
    audio_turn_id: str | None = None,
) -> bool:
    if not qqcall_enabled():
        return False
    try:
        status = await request_qqcall(
            "status",
            platform=platform,
            peer_user_id=peer_user_id,
            scope=scope,
            timeout=_STATUS_TIMEOUT_SECONDS,
        )
    except Exception:
        return False
    call = status.get("call") if isinstance(status, dict) else None
    matched = bool(
        isinstance(status, dict)
        and status.get("peer_user_id") == peer_user_id
        and status.get("scope") == scope
        and isinstance(call, dict)
        and call.get("phase") == "connected"
        and call.get("uncertain") is False
        and call.get("callId") == scope["callId"]
        and call.get("generation") == scope["callGeneration"]
        and (audio_turn_id is None or call.get("audioTurnId") == audio_turn_id)
    )
    return qqcall_enabled() and matched


async def verify_qqcall_realtime_message(
    message: Any,
    chat_id: str,
    annotation: RealtimeSession,
) -> bool:
    """Match the declared binding to the QQ scope, peer, route, and live call."""

    details = _voice_details(message, chat_id, annotation)
    if details is None:
        return False
    peer_user_id, scope, platform = details
    return await _current_call_matches(platform, peer_user_id, scope, annotation.turn_id)


def _lifecycle_details(message: Any, chat_id: str) -> tuple[str, dict[str, Any], dict[str, Any], str] | None:
    info = getattr(message, "message_info", None)
    if info is None or getattr(info, "user_info", None) is not None or _message_user_id(message) is not None:
        return None
    if _message_group_id(message) is not None:
        return None
    event = get_system_event(message)
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type.startswith("qq.call."):
        return None
    config = additional_config_from_message(message)
    try:
        scope = validate_qqcall_scope(config.get("qqcall_scope"))
    except QQCallAPIError:
        return None
    target = config.get("delivery_target")
    route = get_system_event_route(message)
    peer_user_id = target.get("peer_user_id") if isinstance(target, dict) else None
    route_peer = route.get("peer") if isinstance(route, dict) else None
    data = event.get("data")
    platform = _message_platform(message)
    if (
        not qqcall_enabled()
        or platform != "qq"
        or not isinstance(target, dict)
        or target.get("kind") != "qqcall"
        or any(target.get(key) != scope[key] for key in _SCOPE_KEYS)
        or not isinstance(peer_user_id, str)
        or target.get("peer_user_id") != config.get("peer_user_id")
        or not isinstance(route, dict)
        or route.get("kind") != "private"
        or route.get("platform") != platform
        or not isinstance(route_peer, dict)
        or route_peer.get("user_id") != peer_user_id
        or not isinstance(data, dict)
        or data.get("call_id") != scope["callId"]
        or data.get("call_generation") != scope["callGeneration"]
    ):
        return None
    try:
        from src.chat.message_receive.chat_stream import get_chat_manager

        expected_chat_id = get_chat_manager().get_stream_id(platform, peer_user_id, is_group=False)
    except Exception:
        return None
    if expected_chat_id != chat_id:
        return None
    return peer_user_id, scope, event, event_type.removeprefix("qq.call.")


async def verify_qqcall_lifecycle(message: Any, chat_id: str) -> RealtimeLifecycleChange | None:
    """Translate a plugin-verified QQ lifecycle event into an opaque lease change."""

    details = _lifecycle_details(message, chat_id)
    if details is None:
        return None
    peer_user_id, scope, event, kind = details
    session_id = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    data = event.get("data")
    if kind == "call_connected":
        if data.get("uncertain") is True or data.get("phase") in {"end_observed", "failed", "expired", "terminal"}:
            return RealtimeLifecycleChange("qqcall", session_id, chat_id, False)
        if data.get("phase") != "connected" or data.get("uncertain") is not False:
            return None
        if not await _current_call_matches("qq", peer_user_id, scope):
            return RealtimeLifecycleChange("qqcall", session_id, chat_id, False)
        return RealtimeLifecycleChange("qqcall", session_id, chat_id, True, _SESSION_TTL_SECONDS)
    if kind in {"call_terminal", "controller_invalidated", "stopped"}:
        return RealtimeLifecycleChange("qqcall", session_id, chat_id, False)
    return None
