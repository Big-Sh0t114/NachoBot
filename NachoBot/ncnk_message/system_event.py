"""Shared validation helpers for structured platform system events.

The event envelope is deliberately small and JSON-compatible so it can cross
the adapter/Core WebSocket boundary and survive the existing ``additional_config``
storage column without a schema change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


SYSTEM_EVENT_KEY = "system_event"
SYSTEM_EVENT_VERSION = 1
SYSTEM_EVENT_ROUTE_KEY = "system_event_route"
SYSTEM_EVENT_ROUTE_KIND_PRIVATE = "private"
_CANONICAL_ENVELOPE_KEYS = frozenset({"version", "type", "actor", "target", "data"})
_CANONICAL_ROUTE_KEYS = frozenset({"platform", "kind", "peer", "user_id", "name", "nickname", "cardname"})
_CANONICAL_PEER_KEYS = frozenset({"user_id", "name", "nickname", "cardname"})


class SystemEventState(str, Enum):
    """Tri-state result for the optional structured event field."""

    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class SystemEventResult:
    """Validated event plus its presence state.

    ``event`` is only populated for :attr:`SystemEventState.VALID`.  The small
    convenience properties make the result usable by callers that prefer an
    explicit tri-state over truthiness.
    """

    state: SystemEventState
    event: dict[str, Any] | None = None

    @property
    def is_absent(self) -> bool:
        return self.state is SystemEventState.ABSENT

    @property
    def is_valid(self) -> bool:
        return self.state is SystemEventState.VALID

    @property
    def is_invalid(self) -> bool:
        return self.state is SystemEventState.INVALID

    @property
    def value(self) -> dict[str, Any] | None:
        """Alias useful to callers treating this as an accessor result."""

        return self.event

    def __bool__(self) -> bool:
        return self.is_valid


@dataclass(frozen=True, slots=True)
class SystemEventRouteResult:
    """Tri-state result for the optional private stream routing metadata."""

    state: SystemEventState
    route: dict[str, Any] | None = None

    @property
    def is_absent(self) -> bool:
        return self.state is SystemEventState.ABSENT

    @property
    def is_valid(self) -> bool:
        return self.state is SystemEventState.VALID

    @property
    def is_invalid(self) -> bool:
        return self.state is SystemEventState.INVALID

    @property
    def value(self) -> dict[str, Any] | None:
        return self.route

    def __bool__(self) -> bool:
        return self.is_valid


# A few descriptive aliases keep the public package API discoverable without
# creating multiple implementations of the contract.
SystemEventStatus = SystemEventState
SystemEventClassification = SystemEventResult
SystemEventRouteStatus = SystemEventState
SystemEventRouteClassification = SystemEventRouteResult


def _nonempty_party(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(str(value).strip())


def _valid_party(value: Any) -> bool:
    return value is None or (
        isinstance(value, Mapping)
        and (_nonempty_party(value.get("user_id")) or _nonempty_party(value.get("name")))
    )


def _validate_envelope(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if not set(value).issubset(_CANONICAL_ENVELOPE_KEYS):
        return None
    version = value.get("version")
    if isinstance(version, bool) or version != SYSTEM_EVENT_VERSION:
        return None
    event_type = value.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        return None
    if not _valid_party(value.get("actor")) or not _valid_party(value.get("target")):
        return None
    data = value.get("data", {})
    if not isinstance(data, Mapping):
        return None

    # Return a plain dict with exactly the canonical fields.  Mapping inputs
    # from adapters are copied so later mutation cannot alter the envelope that
    # was validated or persisted.
    normalized = {
        "version": SYSTEM_EVENT_VERSION,
        "type": event_type,
        "actor": dict(value["actor"]) if isinstance(value.get("actor"), Mapping) else None,
        "target": dict(value["target"]) if isinstance(value.get("target"), Mapping) else None,
        "data": dict(data),
    }
    # The envelope crosses the adapter boundary and is stored as JSON.  Do
    # this check once at the shared validation boundary so adapters cannot
    # accidentally emit an object that only looks like a mapping in memory.
    try:
        json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return normalized


def build_system_event(
    event_type: str,
    actor: Mapping[str, Any] | None = None,
    target: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    *,
    version: int = SYSTEM_EVENT_VERSION,
) -> dict[str, Any]:
    """Build one canonical v1 event envelope.

    Invalid envelopes are rejected at adapter ingress rather than being sent
    across the wire and later interpreted as ordinary user messages.
    """

    envelope = {
        "version": version,
        "type": event_type,
        "actor": actor,
        "target": target,
        "data": {} if data is None else data,
    }
    validated = _validate_envelope(envelope)
    if validated is None:
        raise ValueError("invalid system_event envelope")
    return validated


def _validate_system_event_route(value: Any) -> dict[str, Any] | None:
    """Validate and normalize the explicit private stream route contract."""

    if not isinstance(value, Mapping):
        return None
    if not set(value).issubset(_CANONICAL_ROUTE_KEYS):
        return None

    platform = value.get("platform")
    kind = value.get("kind")
    peer = value.get("peer")
    # Accept the equally JSON-friendly flat spelling while emitting one
    # canonical nested ``peer`` object for downstream callers.
    if peer is None and "user_id" in value:
        peer = {field: value.get(field) for field in ("user_id", "name", "nickname", "cardname") if field in value}
    if not isinstance(platform, str) or not platform.strip():
        return None
    if kind != SYSTEM_EVENT_ROUTE_KIND_PRIVATE or not isinstance(peer, Mapping):
        return None
    if not set(peer).issubset(_CANONICAL_PEER_KEYS):
        return None

    user_id = peer.get("user_id")
    if user_id is None or isinstance(user_id, bool):
        return None
    user_id = str(user_id).strip()
    if not user_id:
        return None

    normalized_peer: dict[str, Any] = {"user_id": user_id}
    for field in ("name", "nickname", "cardname"):
        field_value = peer.get(field)
        if field_value is None:
            continue
        if not isinstance(field_value, str):
            return None
        # Optional display fields are retained when non-empty.  An empty
        # value carries no identity and is equivalent to omission.
        field_value = field_value.strip()
        if field_value:
            normalized_peer[field] = field_value

    normalized = {
        "platform": platform.strip(),
        "kind": SYSTEM_EVENT_ROUTE_KIND_PRIVATE,
        "peer": normalized_peer,
    }
    try:
        json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return normalized


def build_system_event_route(
    platform: str,
    user_id: Any = None,
    *,
    peer: Mapping[str, Any] | None = None,
    nickname: str | None = None,
    cardname: str | None = None,
    name: str | None = None,
    user_nickname: str | None = None,
    user_cardname: str | None = None,
    kind: str = SYSTEM_EVENT_ROUTE_KIND_PRIVATE,
) -> dict[str, Any]:
    """Build one canonical private stream route.

    ``peer`` may be supplied as the second positional argument for adapter
    convenience.  The public wire shape intentionally uses ``nickname`` and
    ``cardname`` so it remains independent of the legacy ``UserInfo`` field
    names used by Core.
    """

    if peer is None and isinstance(user_id, Mapping):
        peer = user_id
        user_id = None
    if peer is None:
        peer = {"user_id": user_id}
        if nickname is not None:
            peer["nickname"] = nickname
        if cardname is not None:
            peer["cardname"] = cardname
    else:
        peer = dict(peer)
        if user_id is not None:
            peer["user_id"] = user_id
        if nickname is not None:
            peer["nickname"] = nickname
        if cardname is not None:
            peer["cardname"] = cardname
    if user_nickname is not None:
        peer["nickname"] = user_nickname
    if user_cardname is not None:
        peer["cardname"] = user_cardname
    if name is not None:
        peer["name"] = name

    route = {"platform": platform, "kind": kind, "peer": peer}
    validated = _validate_system_event_route(route)
    if validated is None:
        raise ValueError("invalid system_event_route")
    return validated


def _extract_event_candidate(value: Any) -> tuple[bool, Any]:
    """Return ``(present, candidate)`` from a message/config/envelope value."""

    if value is None:
        return False, None

    # MessageRecv / DatabaseMessages / BaseMessageInfo style objects.
    if not isinstance(value, (Mapping, str)):
        message_info = getattr(value, "message_info", None)
        if message_info is not None:
            return _extract_event_candidate(getattr(message_info, "additional_config", None))
        if hasattr(value, "additional_config"):
            return _extract_event_candidate(getattr(value, "additional_config", None))
        # An arbitrary message-like object without an additional_config field
        # is an ordinary value, not evidence that a system event was present.
        return False, None

    if isinstance(value, str):
        if not value.strip():
            return False, None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return True, value
        return _extract_event_candidate(parsed)

    # First unwrap message/config containers.  A normal adapter config may
    # legitimately contain keys such as ``type`` or ``data`` for its own
    # payload; those are not a system event unless the explicit nested field
    # is present.  A nested field is intentionally present-invalid when its
    # value is malformed, so it cannot fall through as an ordinary message.
    if SYSTEM_EVENT_KEY in value:
        return True, value.get(SYSTEM_EVENT_KEY)
    nested_message_info = value.get("message_info")
    if isinstance(nested_message_info, Mapping) and "additional_config" in nested_message_info:
        return _extract_event_candidate(nested_message_info.get("additional_config"))
    if "additional_config" in value:
        return _extract_event_candidate(value.get("additional_config"))

    # An envelope passed directly is recognized only when it has the
    # unambiguous version/type signature.  The validator then rejects any
    # unrelated keys.  In particular, ``{"data": ...}`` and ``{"type": ...}``
    # remain ordinary config payloads.
    if "version" in value and "type" in value:
        return True, value
    return False, None


def _extract_route_candidate(value: Any) -> tuple[bool, Any]:
    """Return ``(present, candidate)`` for explicit route metadata."""

    if value is None:
        return False, None

    if not isinstance(value, (Mapping, str)):
        message_info = getattr(value, "message_info", None)
        if message_info is not None:
            return _extract_route_candidate(getattr(message_info, "additional_config", None))
        if hasattr(value, "additional_config"):
            return _extract_route_candidate(getattr(value, "additional_config", None))
        return False, None

    if isinstance(value, str):
        if not value.strip():
            return False, None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return True, value
        return _extract_route_candidate(parsed)

    if SYSTEM_EVENT_ROUTE_KEY in value:
        return True, value.get(SYSTEM_EVENT_ROUTE_KEY)
    nested_message_info = value.get("message_info")
    if isinstance(nested_message_info, Mapping) and "additional_config" in nested_message_info:
        return _extract_route_candidate(nested_message_info.get("additional_config"))
    if "additional_config" in value:
        return _extract_route_candidate(value.get("additional_config"))

    # A direct route is useful for callers validating a route before placing
    # it beside ``system_event``.  Avoid treating arbitrary config mappings as
    # routes unless the complete signature is present and no unrelated keys
    # exist.
    if (
        (
            {"platform", "kind", "peer"}.issubset(value)
            or {"platform", "kind", "user_id"}.issubset(value)
        )
        and set(value).issubset(_CANONICAL_ROUTE_KEYS)
    ):
        return True, value
    return False, None


def classify_system_event(value: Any) -> SystemEventResult:
    """Classify an optional event as absent, valid, or present-invalid.

    ``value`` may be a canonical envelope, an ``additional_config`` mapping,
    a persisted JSON string containing either, or a message object carrying
    ``message_info.additional_config``.
    """

    present, candidate = _extract_event_candidate(value)
    if not present:
        return SystemEventResult(SystemEventState.ABSENT)
    event = _validate_envelope(candidate)
    if event is None:
        return SystemEventResult(SystemEventState.INVALID)
    return SystemEventResult(SystemEventState.VALID, event)


def get_system_event(value: Any) -> dict[str, Any] | None:
    """Return the validated envelope or ``None`` for absent/invalid input."""

    result = classify_system_event(value)
    return result.event if result.is_valid else None


def classify_system_event_route(value: Any) -> SystemEventRouteResult:
    """Classify explicit routing metadata as absent, valid, or invalid."""

    present, candidate = _extract_route_candidate(value)
    if not present:
        return SystemEventRouteResult(SystemEventState.ABSENT)
    route = _validate_system_event_route(candidate)
    if route is None:
        return SystemEventRouteResult(SystemEventState.INVALID)
    return SystemEventRouteResult(SystemEventState.VALID, route)


def system_event_route_result(value: Any) -> SystemEventRouteResult:
    """Descriptive alias for :func:`classify_system_event_route`."""

    return classify_system_event_route(value)


def get_system_event_route(value: Any) -> dict[str, Any] | None:
    """Return a validated route or ``None`` for absent/invalid metadata."""

    result = classify_system_event_route(value)
    return result.route if result.is_valid else None


def validate_system_event_route(value: Any) -> dict[str, Any] | None:
    """Validate a route mapping without requiring an ``additional_config`` wrapper."""

    return _validate_system_event_route(value)


def system_event_fallback_text(event: Mapping[str, Any] | None) -> str:
    """Return stable readable text when an event has no rendered segment text."""

    event_type = event.get("type") if isinstance(event, Mapping) else None
    event_type = str(event_type or "unknown").strip() or "unknown"
    return f"[系统事件: {event_type}]"


def system_event_result(value: Any) -> SystemEventResult:
    """Descriptive alias for :func:`classify_system_event`."""

    return classify_system_event(value)


__all__ = [
    "SYSTEM_EVENT_KEY",
    "SYSTEM_EVENT_VERSION",
    "SYSTEM_EVENT_ROUTE_KEY",
    "SYSTEM_EVENT_ROUTE_KIND_PRIVATE",
    "SystemEventState",
    "SystemEventStatus",
    "SystemEventResult",
    "SystemEventClassification",
    "SystemEventRouteResult",
    "SystemEventRouteStatus",
    "SystemEventRouteClassification",
    "build_system_event",
    "build_system_event_route",
    "classify_system_event",
    "system_event_result",
    "get_system_event",
    "classify_system_event_route",
    "system_event_route_result",
    "get_system_event_route",
    "validate_system_event_route",
    "system_event_fallback_text",
]
