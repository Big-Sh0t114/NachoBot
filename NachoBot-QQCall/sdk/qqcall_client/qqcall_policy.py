"""Pure admission predicates for explicit QQCall group requests.

This module deliberately contains no adapter, Core, network, or native-provider
dependencies so both the command handler and the shared runtime can use the same
small policy surface.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_GROUP_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_COMMAND_TRIGGER_RE = re.compile(r"#join_vc_qq", re.IGNORECASE | re.ASCII)
_NATURAL_TRIGGER_RE = re.compile(
    r"^[ \u3000]*(?:加入群语音|请加入群语音|加入当前群通话)[ \u3000]*[。！!?.]?[ \u3000]*$"
)
_GROUP_ROOM_KEYS = frozenset({"groupId", "roomId", "roomGeneration", "live", "observedAtMs"})
_GROUP_ROOM_MAX_AGE_MS = 5_000
_GROUP_STATUS_KEYS = frozenset({
    "version", "accountId", "serviceInstance", "hostGeneration",
    "connectionGeneration", "groupId", "occupied", "nativeJoinVerified", "room",
})


def match_group_call_trigger(text: Any) -> str | None:
    """Match only the small, anchored text phrases enabled for group join."""

    if not isinstance(text, str) or not text or len(text) > 128:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        return None
    candidate = text
    if _COMMAND_TRIGGER_RE.fullmatch(candidate):
        return "command"
    if _NATURAL_TRIGGER_RE.fullmatch(candidate):
        return "natural"
    return None


def group_status_decision(
    status: Any,
    *,
    account_id: str,
    service_meta: Mapping[str, Any],
    group_id: str,
    local_call_scope: Mapping[str, Any] | None,
    now_ms: int | None = None,
    expected_room: Mapping[str, Any] | None = None,
) -> str | None:
    """Return a safe refusal code, or ``None`` only for a proven join candidate.

    A status response is fresh only when its lease and process generations match
    the current adapter poll and it names the exact requested group. A positive
    native assertion must include a live room for that same group. The current
    provider contract always returns ``nativeJoinVerified=False`` and ``room=None``.
    """

    if not isinstance(group_id, str) or not _GROUP_ID_RE.fullmatch(group_id):
        return "group_id_invalid"
    if not isinstance(status, Mapping) or set(status) != _GROUP_STATUS_KEYS:
        return "group_status_invalid"
    if (
        type(status.get("version")) is not int
        or status.get("version") != 1
        or status.get("accountId") != account_id
        or not isinstance(status.get("serviceInstance"), str)
        or status.get("serviceInstance") != service_meta.get("serviceInstance")
        or type(status.get("hostGeneration")) is not int
        or status.get("hostGeneration") != service_meta.get("hostGeneration")
        or type(status.get("connectionGeneration")) is not int
        or status.get("connectionGeneration") != service_meta.get("connectionGeneration")
        or status.get("groupId") != group_id
        or type(status.get("occupied")) is not bool
        or type(status.get("nativeJoinVerified")) is not bool
    ):
        return "group_status_stale"

    if status["occupied"] or local_call_scope is not None:
        return "call_occupied"
    if status["nativeJoinVerified"] is not True or not isinstance(status.get("room"), Mapping):
        return "unsupported_unverified_group_join"
    room = status["room"]
    if set(room) != _GROUP_ROOM_KEYS:
        return "unsupported_unverified_group_join"
    room_id = room.get("roomId")
    room_generation = room.get("roomGeneration")
    observed_at_ms = room.get("observedAtMs")
    if (
        room.get("groupId") != group_id
        or not isinstance(room_id, str)
        or not room_id
        or room_id.strip() != room_id
        or len(room_id) > 128
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in room_id)
        or type(room_generation) is not int
        or not 1 <= room_generation <= 2**53 - 1
        or room.get("live") is not True
        or type(observed_at_ms) is not int
        or now_ms is None
        or type(now_ms) is not int
        or not 0 <= now_ms - observed_at_ms <= _GROUP_ROOM_MAX_AGE_MS
    ):
        return "unsupported_unverified_group_join"
    if expected_room is not None:
        expected_identity = {
            key: expected_room.get(key)
            for key in ("groupId", "roomId", "roomGeneration")
        }
        current_identity = {key: room[key] for key in ("groupId", "roomId", "roomGeneration")}
        if expected_identity != current_identity:
            return "group_room_changed"
    return None


__all__ = ["group_status_decision", "match_group_call_trigger"]
