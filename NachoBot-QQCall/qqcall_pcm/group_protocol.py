"""Pure QQ group scope and anonymous-speaker projections for UniversalVC.

This module only validates and projects values supplied by a separately trusted
runtime. A valid-looking scope is not proof that QQ joined an existing room and
does not grant room, ACL, private-slot, audio-owner, or Core authorization.
Callers must independently verify the current native room proof, adapter ACL,
service/connection generations, and exclusive audio ownership before using the
options returned here. In particular, existing-room proof remains mandatory
before any group audio session starts. Speaker projection also requires a
separately validated live UniversalVC service/session/generation/source
identity; a valid-looking identity dict does not prove that it is current.

No session, service, device, network, PCM, or Core objects are created here.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


_SCOPE_FIELDS = (
    "kind",
    "serviceInstance",
    "hostGeneration",
    "connectionGeneration",
    "groupId",
    "roomId",
    "roomGeneration",
)
_SCOPE_KEYS = frozenset(_SCOPE_FIELDS)
_GROUP_KIND = "qqcall_group"
_MAX_GENERATION = 2**53 - 1
_MAX_SERVICE_INSTANCE_LENGTH = 128
_MAX_ROOM_ID_LENGTH = 128
_MAX_SPEAKER_ID_LENGTH = 128
_MAX_UVC_ID_LENGTH = 128
_GROUP_UIN = re.compile(r"[1-9][0-9]{0,19}", re.ASCII)
_UVC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_SPEAKER_REF_HEX_LENGTH = 16
_UVC_SESSION_FIELDS = (
    "service_instance_id",
    "session_id",
    "session_generation",
    "source_id",
)
_UVC_SESSION_KEYS = frozenset(_UVC_SESSION_FIELDS)


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _valid_opaque_id(value: Any, *, maximum_length: int) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum_length
        and value == value.strip()
        and not any(character.isspace() for character in value)
        and not _has_control_character(value)
    )


def _valid_room_id(value: Any) -> bool:
    """Match the existing QQCall room-reference wire checks."""

    return (
        type(value) is str
        and bool(value)
        and value.strip() == value
        and len(value) <= _MAX_ROOM_ID_LENGTH
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def _validated_uvc_session_identity(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _UVC_SESSION_KEYS:
        raise ValueError("UniversalVC session identity is invalid")

    for field in _UVC_SESSION_FIELDS:
        identifier = value.get(field)
        if (
            type(identifier) is not str
            or len(identifier) > _MAX_UVC_ID_LENGTH
            or _UVC_ID.fullmatch(identifier) is None
        ):
            raise ValueError("UniversalVC session identity is invalid")

    if value["source_id"] != "main":
        raise ValueError("UniversalVC session identity is invalid")

    return {field: value[field] for field in _UVC_SESSION_FIELDS}


def validate_group_scope(value: Any) -> dict[str, Any]:
    """Validate an exact QQ group-room scope and return a fresh plain dict.

    Raises ``ValueError`` for any non-plain-dict input, missing/extra key,
    malformed ID, or invalid generation. Generation values must be exact
    positive ``int`` instances, so ``bool`` is rejected.
    """

    if type(value) is not dict or set(value) != _SCOPE_KEYS:
        raise ValueError("group scope must contain the exact protocol fields")

    kind = value.get("kind")
    if type(kind) is not str or kind != _GROUP_KIND:
        raise ValueError("group scope kind is invalid")

    if not _valid_opaque_id(
        value.get("serviceInstance"), maximum_length=_MAX_SERVICE_INSTANCE_LENGTH
    ):
        raise ValueError("group scope service instance is invalid")

    for field in ("hostGeneration", "connectionGeneration", "roomGeneration"):
        generation = value.get(field)
        if type(generation) is not int or not 1 <= generation <= _MAX_GENERATION:
            raise ValueError("group scope generation is invalid")

    group_id = value.get("groupId")
    if type(group_id) is not str or _GROUP_UIN.fullmatch(group_id) is None:
        raise ValueError("group scope group ID is invalid")

    room_id = value.get("roomId")
    if not _valid_room_id(room_id):
        raise ValueError("group scope room ID is invalid")

    # Keep group UIN and the opaque room reference in separate, typed fields.
    # Their text values may coincide; a numeric-looking room reference remains
    # a room reference and is never converted into a QQ identity.
    return {field: value[field] for field in _SCOPE_FIELDS}


def group_session_id(scope: Any) -> str:
    """Return a canonical SHA-256 identity for one validated group room lease."""

    validated = validate_group_scope(scope)
    canonical = json.dumps(
        validated,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def universal_group_options(scope: Any) -> dict[str, Any]:
    """Build fresh frozen UniversalVC options for one already-proven group room.

    The scope hash is the only scope value included in the options. This helper
    does not establish the proof or authorization required to create a session.
    """

    session_id = group_session_id(scope)
    return {
        "platform": "qq",
        "scope": session_id,
        "mode": "pipeline",
        "sources": [
            {
                "id": "main",
                "input": "external",
                "format": "s16le",
                "sample_rate": 16_000,
                "channels": 1,
                "capture": {},
                "identity": {"mode": "speaker"},
            }
        ],
        "output": {"mode": "none"},
        "denoise": False,
        "speaker": True,
        "speaker_log": True,
        "max_duration_seconds": 60,
    }


def anonymous_speaker_annotation(
    scope: Any,
    speaker_id: Any,
    uvc_session_identity: Any,
) -> dict[str, Any]:
    """Project a service speaker token to a bounded, session-local annotation.

    The input token is treated only as a UniversalVC cluster string, including
    when it contains digits. The output contains no UIN, speaker name, caller
    principal, raw UVC identifier, or stable cross-session identity. The caller
    must validate that the explicit UVC session identity belongs to the current
    live event before requesting this projection.
    """

    session_id = group_session_id(scope)
    validated_uvc_identity = _validated_uvc_session_identity(uvc_session_identity)
    if not _valid_opaque_id(speaker_id, maximum_length=_MAX_SPEAKER_ID_LENGTH):
        raise ValueError("UniversalVC speaker ID is invalid")

    canonical = json.dumps(
        ["qqcall-group-speaker-v2", session_id, validated_uvc_identity, speaker_id],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    short_ref = hashlib.sha256(canonical).hexdigest()[:_SPEAKER_REF_HEX_LENGTH]
    return {
        "version": 1,
        "speaker_ref": f"qqg-{short_ref}",
        "label": f"语音用户 {short_ref}",
    }


__all__ = [
    "anonymous_speaker_annotation",
    "group_session_id",
    "universal_group_options",
    "validate_group_scope",
]
