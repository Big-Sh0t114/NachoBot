"""Plugin-owned proof provider for senderless QQCall group voice turns.

The audio-status proof requires the runtime's current native-owned group
binding. This provider publishes no synthetic lifecycle events: each turn and
each Core dispatch rechecks the read-only native proof endpoint, while Core's
bounded lease handles missing lifecycle notifications. Failed ASR display
markers never authorize a conversational turn.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from qqcall_client.qqcall_group_wire import (
    group_audio_session_id,
    validate_group_audio_claim,
    validate_group_audio_scope,
)
from src.plugins.built_in.qqcall_plugin.realtime_provider import qqcall_enabled
from src.chat.runtime_capabilities import additional_config_from_message
from src.plugins.built_in.qqcall_plugin.control_api import request_qqcall
from src.plugins.built_in.qqcall_plugin.transcription import failed_voice_transcription


_SCOPE_KEYS = frozenset(
    {
        "kind",
        "serviceInstance",
        "hostGeneration",
        "connectionGeneration",
        "groupId",
        "roomId",
        "roomGeneration",
    }
)
_TARGET_KEYS = _SCOPE_KEYS | {"kind", "requester_user_id", "turn_id"}
_REALTIME_SESSION_KEYS = frozenset({"version", "provider", "session_id", "turn_id"})
_CAPABILITY_KEYS = frozenset(
    {"schema_version", "planner_bypass", "reply_delivery", "voice_stream", "mcp_enabled", "tts_language"}
)
_SPEAKER_KEYS = frozenset({"version", "speaker_ref", "label"})
_QQ_UIN = re.compile(r"[1-9][0-9]{0,19}\Z", re.ASCII)
_STATUS_TIMEOUT_SECONDS = 2.5
_MAX_ASR_RESULT_ID_BYTES = 256
_MAX_ASR_SCOPE_BYTES = 2048


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


def _bounded_utf8(value: Any, maximum: int) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        return len(value.encode("utf-8", errors="strict")) <= maximum
    except UnicodeError:
        return False


def _message_id(message: Any) -> str:
    info = _field(message, "message_info")
    value = _field(info, "message_id") or _field(message, "message_id")
    return value if type(value) is str else ""


def _has_sender_principal(message: Any) -> bool:
    info = _field(message, "message_info")
    if any(
        _field(container, key) is not None
        for container, key in (
            (message, "user_info"),
            (info, "user_info"),
            (message, "sender_info"),
            (info, "sender_info"),
        )
    ):
        return True
    return any(
        value not in (None, "")
        for value in (_field(message, "user_id"), _field(message, "sender_id"))
    )


def _raw_additional_config(message: Any) -> tuple[Any, ...]:
    info = _field(message, "message_info")
    return (
        _field(message, "additional_config"),
        _field(message, "additional_data"),
        _field(info, "additional_config"),
    )


def _declares_system_event(message: Any, config: Mapping[str, Any]) -> bool:
    if "system_event" in config or _field(message, "system_event") is not None:
        return True
    for raw in _raw_additional_config(message):
        if isinstance(raw, Mapping):
            if "system_event" in raw:
                return True
            continue
        if isinstance(raw, str) and raw.strip():
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(decoded, Mapping) and "system_event" in decoded:
                return True
    return False


def _platform_matches_qq(message: Any) -> bool:
    info = _field(message, "message_info")
    chat_info = _field(message, "chat_info")
    declared = [
        _field(info, "platform"),
        _field(message, "chat_info_platform"),
        _field(chat_info, "platform"),
    ]
    declared = [value for value in declared if value not in (None, "")]
    return bool(declared) and all(value == "qq" for value in declared)


def _group_context_matches(message: Any, scope: dict[str, Any], chat_id: str) -> bool:
    expected_group_id = scope["groupId"]
    info = _field(message, "message_info")
    chat_info = _field(message, "chat_info")
    groups = (
        _field(info, "group_info"),
        _field(message, "group_info"),
        _field(chat_info, "group_info"),
    )
    observed_ids: list[Any] = []
    for group in groups:
        if group is None:
            continue
        group_id = _field(group, "group_id")
        if type(group_id) is not str:
            return False
        observed_ids.append(group_id)
        group_platform = _field(group, "platform") or _field(group, "group_platform")
        if group_platform not in (None, "") and group_platform != "qq":
            return False
    for group_id in (_field(message, "chat_info_group_id"),):
        if group_id not in (None, ""):
            observed_ids.append(group_id)
    if not observed_ids or any(group_id != expected_group_id for group_id in observed_ids):
        return False

    context_ids = (
        _field(message, "chat_id"),
        _field(message, "chat_info_stream_id"),
        _field(chat_info, "stream_id"),
    )
    if any(value not in (None, "", chat_id) for value in context_ids):
        return False
    try:
        from src.chat.message_receive.chat_stream import get_chat_manager

        expected_chat_id = get_chat_manager().get_stream_id(
            "qq", expected_group_id, is_group=True
        )
    except Exception:
        return False
    return expected_chat_id == chat_id


def _is_voice_message(message: Any, expected_message_id: str) -> bool:
    if _field(message, "reply") is not None:
        return False
    segment = _field(message, "message_segment")
    if segment is not None:
        return _field(segment, "type") == "voice"
    is_voice = _field(message, "is_voice")
    if is_voice is True:
        return True
    if is_voice is False:
        return False
    # The persisted Core row has no segment object.  Its adapter-generated ID
    # and native proof binding preserve the exact voice-row declaration.
    return _message_id(message) == expected_message_id


def _speaker_annotation_matches(value: Any, claim: dict[str, Any]) -> dict[str, Any] | None:
    if type(value) is not dict or set(value) != _SPEAKER_KEYS:
        return None
    version = value.get("version")
    speaker_ref = value.get("speaker_ref")
    label = value.get("label")
    if (
        type(version) is not int
        or version != 1
        or type(speaker_ref) is not str
        or speaker_ref != claim["speaker_ref"]
        or type(label) is not str
        or label != f"语音用户 {speaker_ref.removeprefix('qqg-')}"
    ):
        return None
    return {"version": 1, "speaker_ref": speaker_ref, "label": label}


def _proof_details(message: Any, chat_id: str, annotation: Any) -> dict[str, Any] | None:
    if not qqcall_enabled() or _has_sender_principal(message):
        return None
    if not _platform_matches_qq(message):
        return None

    try:
        config = additional_config_from_message(message)
    except Exception:
        return None
    if not isinstance(config, Mapping) or _declares_system_event(message, config):
        return None

    try:
        scope = validate_group_audio_scope(config.get("qqcall_group_scope"))
        claim = validate_group_audio_claim(config.get("qqcall_group_audio_claim"))
    except (TypeError, ValueError):
        return None
    scope_hash = group_audio_session_id(scope)

    requester = config.get("qqcall_group_requester_user_id")
    if type(requester) is not str or _QQ_UIN.fullmatch(requester) is None:
        return None
    raw_target = config.get("delivery_target")
    if type(raw_target) is not dict or set(raw_target) != _TARGET_KEYS:
        return None
    target_scope = {key: raw_target.get(key) for key in _SCOPE_KEYS}
    try:
        target_scope = validate_group_audio_scope(target_scope)
    except (TypeError, ValueError):
        return None
    turn_id = claim["turn_id"]
    if (
        target_scope != scope
        or raw_target.get("kind") != "qqcall_group"
        or raw_target.get("requester_user_id") != requester
        or raw_target.get("turn_id") != turn_id
    ):
        return None

    raw_realtime = config.get("realtime_session")
    if type(raw_realtime) is not dict or set(raw_realtime) != _REALTIME_SESSION_KEYS:
        return None
    if (
        type(raw_realtime.get("version")) is not int
        or raw_realtime.get("version") != 1
        or raw_realtime.get("provider") != "qqcall_group"
        or raw_realtime.get("session_id") != scope_hash
        or raw_realtime.get("turn_id") != turn_id
        or _field(annotation, "version") != 1
        or type(_field(annotation, "version")) is not int
        or _field(annotation, "provider") != "qqcall_group"
        or _field(annotation, "session_id") != scope_hash
        or _field(annotation, "turn_id") != turn_id
    ):
        return None

    speaker = _speaker_annotation_matches(config.get("realtime_speaker"), claim)
    if speaker is None:
        return None
    capabilities = config.get("runtime_capabilities")
    if (
        type(capabilities) is not dict
        or set(capabilities) != _CAPABILITY_KEYS
        or type(capabilities.get("schema_version")) is not int
        or capabilities.get("schema_version") != 1
        or capabilities.get("planner_bypass") is not True
        or capabilities.get("reply_delivery") != "tts_text"
        or capabilities.get("voice_stream") is not True
        or capabilities.get("mcp_enabled") is not False
        or capabilities.get("tts_language") != "zh"
    ):
        return None

    result_id = config.get("precomputed_asr_result_id")
    asr_scope = config.get("precomputed_asr_scope")
    receipt_valid = _bounded_utf8(result_id, _MAX_ASR_RESULT_ID_BYTES) and _bounded_utf8(
        asr_scope, _MAX_ASR_SCOPE_BYTES)
    # A receipt speeds up ASR; it is not the native call authorization proof.
    # Fallback still requires the identical scope, turn, speaker, ACL and lease.
    fallback_valid = result_id is None and _bounded_utf8(asr_scope, _MAX_ASR_SCOPE_BYTES)
    if not (receipt_valid or fallback_valid):
        return None

    message_id = _message_id(message)
    expected_message_id = f"qqcall:group-voice:{scope_hash}:{turn_id}"
    if message_id != expected_message_id or not _is_voice_message(message, expected_message_id):
        return None
    if not _group_context_matches(message, scope, chat_id):
        return None

    return {
        "scope": scope,
        "scope_hash": scope_hash,
        "claim": claim,
        "requester": requester,
        "turn_id": turn_id,
        "speaker": speaker,
        "message_id": message_id,
        "chat_id": chat_id,
        "asr_result_id": result_id,
        "asr_scope": asr_scope,
        "capabilities": dict(capabilities),
    }


async def verify_qqcall_group_realtime_message(
    message: Any,
    chat_id: str,
    annotation: Any,
) -> bool:
    """Require an exact senderless QQ group row and a current native proof."""

    if failed_voice_transcription(message):
        return False
    details = _proof_details(message, chat_id, annotation)
    if details is None:
        return False
    try:
        status = await request_qqcall(
            "group_audio_status",
            platform="qq",
            peer_user_id=details["requester"],
            group_id=details["scope"]["groupId"],
            scope_group=dict(details["scope"]),
            audio_claim={
                "turn_id": details["claim"]["turn_id"],
                "uvc_session": dict(details["claim"]["uvc_session"]),
                "speaker_ref": details["claim"]["speaker_ref"],
            },
            timeout=_STATUS_TIMEOUT_SECONDS,
        )
    except Exception:
        return False

    # Re-read every local binding field after the await.  A scope or requester
    # mutation during the API call invalidates the proof instead of authorizing
    # a row assembled from two different snapshots.
    if not qqcall_enabled() or failed_voice_transcription(message):
        return False
    current = _proof_details(message, chat_id, annotation)
    if current != details:
        return False
    return type(status) is dict and set(status) == {"active"} and status.get("active") is True


__all__ = ["verify_qqcall_group_realtime_message"]
