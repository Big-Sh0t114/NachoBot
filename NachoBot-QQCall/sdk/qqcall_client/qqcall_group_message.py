"""Build senderless Core messages from trusted QQCall group speech callbacks.

UniversalVC owns speech recognition and speaker clustering.  This adapter
copies only the bounded scope, turn, audio, optional receipt, and anonymous-speaker
projection into Core's existing QQ group context. Core resolves a receipt or
uses ordinary ASR on the exact complete WAV when streaming ASR has no receipt.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from typing import Any

from ncnk_message.message_base import BaseMessageInfo, GroupInfo, MessageBase, Seg
from .qqcall_group_wire import (
    group_audio_session_id,
    validate_group_audio_claim,
    validate_group_audio_scope,
)


_ENVELOPE_KEYS = frozenset(
    {
        "type",
        "scope",
        "session_ref",
        "session_identity",
        "source_id",
        "speaker_annotation",
        "speech_result",
    }
)
_SESSION_KEYS = frozenset(
    {"service_instance_id", "session_id", "session_generation", "source_id"}
)
_SPEAKER_KEYS = frozenset({"version", "speaker_ref", "label"})
_SPEECH_RESULT_KEYS = frozenset(
    {
        "turn_id",
        "text",
        "voice_base64",
        "precomputed_asr_result_id",
        "asr_scope",
    }
)
_QQ_UIN = re.compile(r"[1-9][0-9]{0,19}\Z", re.ASCII)
_MAX_TRANSCRIPT_BYTES = 16 * 1024
_MAX_ASR_RESULT_ID_BYTES = 256
_MAX_ASR_SCOPE_BYTES = 2048
_MAX_SPEECH_WAV_BYTES = 60 * 16_000 * 2 + 4096
_MAX_SPEECH_WAV_BASE64_CHARS = ((_MAX_SPEECH_WAV_BYTES + 2) // 3) * 4
_SPEAKER_LABEL_PREFIX = "语音用户 "


def _bounded_utf8(value: Any, maximum: int) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        return len(value.encode("utf-8", errors="strict")) <= maximum
    except UnicodeError:
        return False


def _validate_voice(value: Any) -> str:
    if type(value) is not str or not value or len(value) > _MAX_SPEECH_WAV_BASE64_CHARS:
        raise ValueError("group speech audio is invalid")
    try:
        encoded = value.encode("ascii", errors="strict")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeError, binascii.Error, ValueError):
        raise ValueError("group speech audio is invalid") from None
    if not decoded or len(decoded) > _MAX_SPEECH_WAV_BYTES:
        raise ValueError("group speech audio is invalid")
    # Return the exact callback value.  Validation must not rewrite the WAV.
    return value


def _validate_speaker_annotation(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _SPEAKER_KEYS:
        raise ValueError("group speaker annotation is invalid")
    version = value.get("version")
    speaker_ref = value.get("speaker_ref")
    label = value.get("label")
    if (
        type(version) is not int
        or version != 1
        or type(speaker_ref) is not str
        or not _bounded_utf8(speaker_ref, 32)
        or not speaker_ref.startswith("qqg-")
        or type(label) is not str
        or label != f"{_SPEAKER_LABEL_PREFIX}{speaker_ref[4:]}"
    ):
        raise ValueError("group speaker annotation is invalid")
    return {"version": 1, "speaker_ref": speaker_ref, "label": label}


def build_qqcall_group_voice_message(
    envelope: Any,
    requester_user_id: str,
    group_name: str,
) -> MessageBase:
    """Validate and copy one callback result into an actorless QQ group voice.

    ``requester_user_id`` is the real command requester used later for the
    QQCall ACL check.  It is metadata only and is never installed as a message
    sender or a speaker identity.
    """

    if type(envelope) is not dict or set(envelope) != _ENVELOPE_KEYS:
        raise ValueError("group speech envelope is invalid")
    if envelope.get("type") != "qqcall_group_speech_result":
        raise ValueError("group speech envelope is invalid")
    if type(requester_user_id) is not str or _QQ_UIN.fullmatch(requester_user_id) is None:
        raise ValueError("group speech requester is invalid")
    if group_name is not None and (type(group_name) is not str or len(group_name) > 256):
        raise ValueError("group name is invalid")

    try:
        scope = validate_group_audio_scope(envelope.get("scope"))
        scope_hash = group_audio_session_id(scope)
    except (TypeError, ValueError):
        raise ValueError("group speech scope is invalid") from None
    if envelope.get("session_ref") != scope_hash:
        raise ValueError("group speech session reference is invalid")

    session_identity = envelope.get("session_identity")
    if type(session_identity) is not dict or set(session_identity) != _SESSION_KEYS:
        raise ValueError("group speech session identity is invalid")
    speech_result = envelope.get("speech_result")
    if type(speech_result) is not dict or set(speech_result) != _SPEECH_RESULT_KEYS:
        raise ValueError("group speech result is invalid")
    if envelope.get("source_id") != "main" or session_identity.get("source_id") != "main":
        raise ValueError("group speech source is invalid")

    turn_id = speech_result.get("turn_id")
    speaker_annotation = _validate_speaker_annotation(envelope.get("speaker_annotation"))
    try:
        audio_claim = validate_group_audio_claim(
            {
                "turn_id": turn_id,
                "uvc_session": session_identity,
                "speaker_ref": speaker_annotation["speaker_ref"],
            }
        )
    except (TypeError, ValueError):
        raise ValueError("group speech claim is invalid") from None

    transcript = speech_result.get("text")
    voice_base64 = _validate_voice(speech_result.get("voice_base64"))
    result_id = speech_result.get("precomputed_asr_result_id")
    asr_scope = speech_result.get("asr_scope")
    receipt_valid = (
        _bounded_utf8(transcript, _MAX_TRANSCRIPT_BYTES) and bool(transcript.strip())
        and _bounded_utf8(result_id, _MAX_ASR_RESULT_ID_BYTES)
        and _bounded_utf8(asr_scope, _MAX_ASR_SCOPE_BYTES)
    )
    fallback_valid = (
        result_id is None and transcript in (None, "")
        and _bounded_utf8(asr_scope, _MAX_ASR_SCOPE_BYTES)
    )
    if not (receipt_valid or fallback_valid):
        raise ValueError("group speech ASR receipt is invalid")

    copied_speaker = dict(speaker_annotation)
    additional_config = {
        "qqcall_group_scope": dict(scope),
        "qqcall_group_requester_user_id": requester_user_id,
        "qqcall_group_audio_claim": {
            "turn_id": audio_claim["turn_id"],
            "uvc_session": dict(audio_claim["uvc_session"]),
            "speaker_ref": audio_claim["speaker_ref"],
        },
        "realtime_speaker": copied_speaker,
        "delivery_target": {
            "kind": "qqcall_group",
            **dict(scope),
            "requester_user_id": requester_user_id,
            "turn_id": turn_id,
        },
        "realtime_session": {
            "version": 1,
            "provider": "qqcall_group",
            "session_id": scope_hash,
            "turn_id": turn_id,
        },
        "runtime_capabilities": {
            "mcp_enabled": False,
            "schema_version": 1,
            "planner_bypass": True,
            "reply_delivery": "tts_text",
            "voice_stream": True,
            "tts_language": "zh",
        },
    }
    if receipt_valid:
        additional_config["precomputed_asr_result_id"] = result_id
    additional_config["precomputed_asr_scope"] = asr_scope
    message_id = f"qqcall:group-voice:{scope_hash}:{turn_id}"
    return MessageBase(
        message_info=BaseMessageInfo(
            platform="qq",
            message_id=message_id,
            time=time.time(),
            group_info=GroupInfo(
                platform="qq",
                group_id=scope["groupId"],
                group_name=group_name or "",
            ),
            user_info=None,
            sender_info=None,
            receiver_info=None,
            additional_config=additional_config,
        ),
        message_segment=Seg(type="voice", data=voice_base64),
        raw_message=None,
    )


__all__ = ["build_qqcall_group_voice_message"]
