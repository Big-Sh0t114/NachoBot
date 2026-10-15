"""Shared, read-only wire validation for QQCall group audio proofs.

This module validates protocol values only. It does not create audio sessions,
grant native-room authority, or map anonymous speakers to QQ user identities.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


_TURN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_UVC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_SPEAKER_REF = re.compile(r"qqg-[0-9a-f]{16}\Z", re.ASCII)
_UVC_SESSION_FIELDS = (
    "service_instance_id",
    "session_id",
    "session_generation",
    "source_id",
)
_UVC_SESSION_KEYS = frozenset(_UVC_SESSION_FIELDS)
_CLAIM_KEYS = frozenset({"turn_id", "uvc_session", "speaker_ref"})
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
_MAX_GENERATION = 2**53 - 1
_GROUP_ID = re.compile(r"[1-9][0-9]{0,19}\Z", re.ASCII)


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _valid_opaque_id(value: Any, maximum_length: int) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum_length
        and value == value.strip()
        and not any(character.isspace() for character in value)
        and not _has_control_character(value)
    )


def _valid_room_id(value: Any) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value.strip() == value
        and len(value) <= 128
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def validate_group_audio_scope(value: Any) -> dict[str, Any]:
    """Validate the frozen seven-field group scope and return a fresh dict."""

    if type(value) is not dict or set(value) != _SCOPE_KEYS:
        raise ValueError("group audio scope is invalid")
    if type(value.get("kind")) is not str or value["kind"] != "qqcall_group":
        raise ValueError("group audio scope is invalid")
    if not _valid_opaque_id(value.get("serviceInstance"), 128):
        raise ValueError("group audio scope is invalid")
    for field in ("hostGeneration", "connectionGeneration", "roomGeneration"):
        generation = value.get(field)
        if type(generation) is not int or not 1 <= generation <= _MAX_GENERATION:
            raise ValueError("group audio scope is invalid")
    if type(value.get("groupId")) is not str or _GROUP_ID.fullmatch(value["groupId"]) is None:
        raise ValueError("group audio scope is invalid")
    if not _valid_room_id(value.get("roomId")):
        raise ValueError("group audio scope is invalid")
    return {field: value[field] for field in _SCOPE_FIELDS}


def group_audio_session_id(scope: Any) -> str:
    """Hash the validated scope using the canonical group-protocol encoding."""

    validated = validate_group_audio_scope(scope)
    canonical = json.dumps(
        validated,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_group_audio_claim(value: Any) -> dict[str, Any]:
    """Validate an exact anonymous UVC turn claim and return a fresh copy."""

    if type(value) is not dict or set(value) != _CLAIM_KEYS:
        raise ValueError("group audio claim is invalid")
    turn_id = value.get("turn_id")
    speaker_ref = value.get("speaker_ref")
    uvc_session = value.get("uvc_session")
    if (
        type(turn_id) is not str
        or _TURN_ID.fullmatch(turn_id) is None
        or type(speaker_ref) is not str
        or _SPEAKER_REF.fullmatch(speaker_ref) is None
        or type(uvc_session) is not dict
        or set(uvc_session) != _UVC_SESSION_KEYS
    ):
        raise ValueError("group audio claim is invalid")

    normalized_session: dict[str, Any] = {}
    for field in _UVC_SESSION_FIELDS:
        identifier = uvc_session.get(field)
        if type(identifier) is not str or _UVC_ID.fullmatch(identifier) is None:
            raise ValueError("group audio claim is invalid")
        normalized_session[field] = identifier
    if normalized_session["source_id"] != "main":
        raise ValueError("group audio claim is invalid")

    return {
        "turn_id": turn_id,
        "uvc_session": normalized_session,
        "speaker_ref": speaker_ref,
    }


__all__ = [
    "group_audio_session_id",
    "validate_group_audio_claim",
    "validate_group_audio_scope",
]
