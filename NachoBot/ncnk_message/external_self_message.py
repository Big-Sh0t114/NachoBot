"""Shared contract for passive messages emitted by the logged-in platform account.

These messages belong in conversation history, but they must never behave like
new user input.  The adapter validates the active account before constructing
the event; Core and context consumers use the same narrow validator to keep the
message passive after persistence.
"""

from __future__ import annotations

import asyncio
import math
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

from .system_event import build_system_event, get_system_event, get_system_event_route


EXTERNAL_SELF_MESSAGE_SUFFIX = ".external_self_message"
EXTERNAL_SELF_MESSAGE_MARKER = "（不是你发送的信息，可能是用户操作）"
_VALID_ROUTE_KINDS = frozenset({"private", "group"})
_REGISTRY_TTL_SECONDS = 300.0
_UNCERTAIN_SEND_GRACE_SECONDS = 5.0
_MAX_PENDING_SENDS = 512
_MAX_CONFIRMED_IDS = 2048
_MAX_SEEN_IDS = 4096
_MAX_SENDER_NAME_LENGTH = 160


def _plain_id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        result = str(value)
    elif isinstance(value, str):
        result = value.strip()
    else:
        return None
    if not result or len(result) > 256 or any(ord(char) < 32 for char in result):
        return None
    return result


def sanitize_external_self_sender_name(value: Any) -> str | None:
    """Return a short display name without serializing non-string objects."""

    if not isinstance(value, str):
        return None
    normalized = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in value
    )
    result = " ".join(normalized.split())[:_MAX_SENDER_NAME_LENGTH].rstrip()
    return result or None


def external_self_sender_name(event: Any) -> str:
    """Choose the sanitized event display name, falling back to its account ID."""

    data = event.get("data") if isinstance(event, Mapping) else None
    if not isinstance(data, Mapping):
        return ""

    if name := sanitize_external_self_sender_name(data.get("sender_name")):
        return name

    self_id = _plain_id(data.get("self_id"))
    return self_id or ""


def strip_external_self_message_marker(value: Any) -> str:
    """Remove one leading storage marker for display, preserving later occurrences."""

    if not isinstance(value, str):
        return ""
    if value.startswith(EXTERNAL_SELF_MESSAGE_MARKER):
        return value[len(EXTERNAL_SELF_MESSAGE_MARKER) :]
    return value


def _positive_time(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


def _message_info(value: Any) -> Any:
    info = _mapping_value(value, "message_info")
    return info if info is not None else value


def _info_value(value: Any, key: str) -> Any:
    info = _message_info(value)
    candidate = _mapping_value(info, key)
    if candidate is not None:
        return candidate
    # DatabaseMessages stores flattened message IDs/times plus a nested
    # DatabaseChatInfo for the stable platform/group route.
    if key == "platform":
        chat_info = _mapping_value(value, "chat_info")
        return _mapping_value(chat_info, "platform") or _mapping_value(value, "chat_info_platform")
    if key == "group_info":
        group_info = _mapping_value(value, "group_info")
        if group_info is not None:
            return group_info
        chat_info = _mapping_value(value, "chat_info")
        group_info = _mapping_value(chat_info, "group_info")
        if group_info is not None:
            return group_info
        group_id = _mapping_value(value, "chat_info_group_id")
        return {"group_id": group_id} if group_id is not None else None
    return candidate


def _plain_text(value: Any) -> str:
    text = _mapping_value(value, "processed_plain_text")
    if isinstance(text, str) and text.startswith(EXTERNAL_SELF_MESSAGE_MARKER):
        return text

    segment = _mapping_value(value, "message_segment")
    segment_type = _mapping_value(segment, "type")
    segment_data = _mapping_value(segment, "data")
    if segment_type == "seglist" and isinstance(segment_data, (list, tuple)) and segment_data:
        first = segment_data[0]
        if _mapping_value(first, "type") == "text":
            first_data = _mapping_value(first, "data")
            if isinstance(first_data, str) and first_data == EXTERNAL_SELF_MESSAGE_MARKER:
                return first_data
    return ""


def build_external_self_message_event(
    platform: str,
    *,
    self_id: Any,
    message_id: Any,
    route_kind: str,
    peer_id: Any,
    original_timestamp: Any,
    source: str = "live",
    sender_name: Any = None,
) -> dict[str, Any]:
    """Build a senderless passive event for one verified self message."""

    platform_name = str(platform or "").strip()
    account_id = _plain_id(self_id)
    platform_message_id = _plain_id(message_id)
    route_id = _plain_id(peer_id)
    stamp = _positive_time(original_timestamp)
    kind = str(route_kind or "").strip().lower()
    source_name = str(source or "live").strip().lower()
    if (
        not platform_name
        or not account_id
        or not platform_message_id
        or not route_id
        or stamp is None
        or kind not in _VALID_ROUTE_KINDS
        or source_name not in {"live", "offline_history"}
        or (kind == "private" and route_id == account_id)
    ):
        raise ValueError("invalid external self message identity")

    event_data = {
        "platform": platform_name,
        "self_id": account_id,
        "message_id": platform_message_id,
        "route_kind": kind,
        "peer_id": route_id,
        "original_timestamp": stamp,
        "source": source_name,
    }
    event_data["sender_name"] = sanitize_external_self_sender_name(sender_name) or (
        sanitize_external_self_sender_name(account_id) or account_id
    )

    return build_system_event(
        f"{platform_name}{EXTERNAL_SELF_MESSAGE_SUFFIX}",
        actor=None,
        target=None,
        data=event_data,
    )


def _external_event_parts(value: Any) -> tuple[dict[str, Any], Any, Any, Any] | None:
    event = get_system_event(value)
    if event is None or event.get("actor") is not None:
        return None
    event_type = event.get("type")
    data = event.get("data")
    if not isinstance(event_type, str) or not isinstance(data, Mapping):
        return None
    platform = str(data.get("platform") or "").strip()
    if not platform or event_type not in {
        f"{platform}{EXTERNAL_SELF_MESSAGE_SUFFIX}",
        f"platform{EXTERNAL_SELF_MESSAGE_SUFFIX}",
    }:
        return None
    account_id = _plain_id(data.get("self_id"))
    message_id = _plain_id(data.get("message_id"))
    route_id = _plain_id(data.get("peer_id"))
    stamp = _positive_time(data.get("original_timestamp"))
    route_kind = str(data.get("route_kind") or "").strip().lower()
    source = str(data.get("source") or "").strip().lower()
    if (
        account_id is None
        or message_id is None
        or route_id is None
        or stamp is None
        or route_kind not in _VALID_ROUTE_KINDS
        or source not in {"live", "offline_history"}
        or (route_kind == "private" and route_id == account_id)
    ):
        return None

    info = _message_info(value)
    info_message_id = _plain_id(_info_value(value, "message_id"))
    info_platform = str(_info_value(value, "platform") or "").strip()
    raw_stamp = _positive_time(_info_value(value, "time"))
    if info_message_id != message_id or info_platform != platform or raw_stamp != stamp:
        return None
    if _info_value(value, "user_info") is not None or _info_value(value, "sender_info") is not None:
        return None
    if not _plain_text(value):
        return None

    group_info = _info_value(value, "group_info")
    additional = _info_value(value, "additional_config")
    if isinstance(additional, str):
        import json

        try:
            additional = json.loads(additional)
        except (TypeError, ValueError):
            return None
    if not isinstance(additional, Mapping):
        return None
    if route_kind == "private":
        route = get_system_event_route(additional)
        if (
            route is None
            or route.get("platform") != platform
            or route.get("kind") != "private"
            or str((route.get("peer") or {}).get("user_id") or "") != route_id
            or group_info is not None
        ):
            return None
    else:
        group_id = _plain_id(_mapping_value(group_info, "group_id"))
        if group_id != route_id:
            return None

    return event, info, additional, group_info


def is_external_self_message_event(value: Any) -> bool:
    """Return whether the structured event envelope has the passive event type."""

    event = get_system_event(value)
    if event is None:
        return False
    event_type = event.get("type")
    data = event.get("data")
    platform = str(data.get("platform") or "").strip() if isinstance(data, Mapping) else ""
    return bool(
        platform
        and isinstance(event_type, str)
        and event_type
        in {
            f"{platform}{EXTERNAL_SELF_MESSAGE_SUFFIX}",
            f"platform{EXTERNAL_SELF_MESSAGE_SUFFIX}",
        }
    )


def is_external_self_message(value: Any, *, expected_self_id: Any = None) -> bool:
    """Validate a persisted or in-flight passive event including route and marker."""

    parts = _external_event_parts(value)
    if parts is None:
        return False
    event = parts[0]
    expected = _plain_id(expected_self_id) if expected_self_id is not None else None
    return expected is None or str(event["data"]["self_id"]) == expected


@dataclass(frozen=True, slots=True)
class _Scope:
    platform: str
    generation: str
    account_id: str
    route_kind: str
    peer_id: str


@dataclass(frozen=True, slots=True)
class _Pending:
    scope: _Scope
    expires_at: float


class OutboundEchoRegistry:
    """Bounded, connection-scoped exact-ID correlation for self-message echoes."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._pending: dict[str, _Pending] = {}
        self._pending_by_scope: dict[_Scope, OrderedDict[str, None]] = {}
        # Uncertainty belongs to an individual send.  A successful response
        # for another concurrent send on the same route cannot resolve it.
        self._uncertain: OrderedDict[str, tuple[_Scope, float]] = OrderedDict()
        self._confirmed: OrderedDict[tuple[_Scope, str], float] = OrderedDict()
        self._seen: OrderedDict[tuple[_Scope, str], float] = OrderedDict()
        self._changed: dict[_Scope, asyncio.Event] = {}

    @staticmethod
    def _scope(
        platform: Any,
        generation: Any,
        account_id: Any,
        route_kind: Any,
        peer_id: Any,
    ) -> _Scope | None:
        values = tuple(_plain_id(item) for item in (platform, generation, account_id, peer_id))
        kind = str(route_kind or "").strip().lower()
        if any(value is None for value in values) or kind not in _VALID_ROUTE_KINDS:
            return None
        return _Scope(values[0], values[1], values[2], kind, values[3])  # type: ignore[arg-type]

    def _event_for(self, scope: _Scope) -> asyncio.Event:
        return self._changed.setdefault(scope, asyncio.Event())

    def _prune(self) -> None:
        now = self._clock()
        for token, pending in tuple(self._pending.items()):
            if now >= pending.expires_at:
                self._remove_pending(token, pending)
        for cache in (self._confirmed, self._seen):
            for key, expires_at in tuple(cache.items()):
                if now >= expires_at:
                    cache.pop(key, None)
        for token, (_scope, expires_at) in tuple(self._uncertain.items()):
            if now >= expires_at:
                self._uncertain.pop(token, None)
        self._trim(self._confirmed, _MAX_CONFIRMED_IDS)
        self._trim(self._seen, _MAX_SEEN_IDS)

    @staticmethod
    def _trim(cache: OrderedDict, limit: int) -> None:
        while len(cache) > limit:
            cache.popitem(last=False)

    def _remove_pending(self, token: str, pending: _Pending) -> None:
        self._pending.pop(token, None)
        scoped = self._pending_by_scope.get(pending.scope)
        has_scope_pending = False
        if scoped is not None:
            scoped.pop(token, None)
            if not scoped:
                self._pending_by_scope.pop(pending.scope, None)
            else:
                has_scope_pending = True
        event = self._changed.get(pending.scope)
        if event is not None:
            event.set()
        if not has_scope_pending:
            self._changed.pop(pending.scope, None)

    def register(
        self,
        platform: Any,
        generation: Any,
        account_id: Any,
        route_kind: Any,
        peer_id: Any,
        *,
        timeout_sec: float,
    ) -> str | None:
        """Register a send immediately before its transport write."""

        self._prune()
        scope = self._scope(platform, generation, account_id, route_kind, peer_id)
        try:
            lifetime = float(timeout_sec)
        except (TypeError, ValueError, OverflowError):
            return None
        if scope is None or not math.isfinite(lifetime) or lifetime <= 0:
            return None
        if len(self._pending) >= _MAX_PENDING_SENDS:
            oldest_token = next(iter(self._pending))
            self._remove_pending(oldest_token, self._pending[oldest_token])
        token = uuid4().hex
        pending = _Pending(scope, self._clock() + min(lifetime + 1.0, _REGISTRY_TTL_SECONDS))
        self._pending[token] = pending
        self._pending_by_scope.setdefault(scope, OrderedDict())[token] = None
        # A fresh event makes registrations after an idle period visible to waiters.
        changed = self._changed.get(scope)
        if changed is None or changed.is_set():
            self._changed[scope] = asyncio.Event()
        return token

    def resolve(self, token: Any, message_id: Any = None, *, success: bool = False) -> bool:
        """Resolve one action response; only successful exact IDs are suppressible."""

        self._prune()
        token_text = str(token or "").strip()
        pending = self._pending.get(token_text)
        if pending is None:
            return False
        normalized_id = _plain_id(message_id) if success else None
        if normalized_id:
            key = (pending.scope, normalized_id)
            self._confirmed[key] = self._clock() + _REGISTRY_TTL_SECONDS
            self._confirmed.move_to_end(key)
            self._trim(self._confirmed, _MAX_CONFIRMED_IDS)
        self._remove_pending(token_text, pending)
        return bool(normalized_id)

    def mark_uncertain(self, token: Any, *, grace_sec: float = _UNCERTAIN_SEND_GRACE_SECONDS) -> bool:
        """Keep unmatched self events ambiguous briefly after an unconfirmed write."""

        self._prune()
        token_text = str(token or "").strip()
        pending = self._pending.get(token_text)
        if pending is None:
            return False
        try:
            lifetime = float(grace_sec)
        except (TypeError, ValueError, OverflowError):
            lifetime = _UNCERTAIN_SEND_GRACE_SECONDS
        if not math.isfinite(lifetime) or lifetime <= 0:
            lifetime = _UNCERTAIN_SEND_GRACE_SECONDS
        self._remove_pending(token_text, pending)
        self._uncertain[token_text] = (
            pending.scope,
            self._clock() + min(lifetime, _REGISTRY_TTL_SECONDS),
        )
        self._uncertain.move_to_end(token_text)
        self._trim(self._uncertain, _MAX_PENDING_SENDS)
        return True

    async def classify_event(
        self,
        platform: Any,
        generation: Any,
        account_id: Any,
        route_kind: Any,
        peer_id: Any,
        message_id: Any,
    ) -> str:
        """Wait off-reader for same-route sends, then classify by exact ID only."""

        scope = self._scope(platform, generation, account_id, route_kind, peer_id)
        event_id = _plain_id(message_id)
        if scope is None or event_id is None:
            return "external"
        key = (scope, event_id)
        while True:
            self._prune()
            if key in self._confirmed:
                self._confirmed.move_to_end(key)
                return "outbound"
            if key in self._seen:
                self._seen.move_to_end(key)
                return "duplicate"
            pending_tokens = self._pending_by_scope.get(scope)
            if not pending_tokens:
                has_uncertain_send = any(
                    uncertain_scope == scope and self._clock() < uncertain_until
                    for uncertain_scope, uncertain_until in self._uncertain.values()
                )
                if has_uncertain_send:
                    return "uncertain"
                self._seen[key] = self._clock() + _REGISTRY_TTL_SECONDS
                self._trim(self._seen, _MAX_SEEN_IDS)
                return "external"
            pending_times = [self._pending[token].expires_at for token in pending_tokens if token in self._pending]
            if not pending_times:
                continue
            wait_seconds = max(0.001, min(pending_times) - self._clock())
            changed = self._event_for(scope)
            try:
                await asyncio.wait_for(changed.wait(), timeout=wait_seconds)
            except TimeoutError:
                # Expired/abandoned actions cease to own an echo.  The next
                # iteration then admits an unmatched event as external input.
                pass
            if self._changed.get(scope) is changed:
                self._changed[scope] = asyncio.Event()

    def invalidate_platform(self, platform: Any) -> None:
        """Forget stale generations/accounts when an adapter connection changes."""

        platform_name = str(platform or "").strip()
        for token, pending in tuple(self._pending.items()):
            if pending.scope.platform == platform_name:
                self._remove_pending(token, pending)
        for cache in (self._confirmed, self._seen):
            for key in tuple(cache):
                if key[0].platform == platform_name:
                    cache.pop(key, None)
        for token, (scope, _expires_at) in tuple(self._uncertain.items()):
            if scope.platform == platform_name:
                self._uncertain.pop(token, None)
        for scope in tuple(self._changed):
            if scope.platform == platform_name:
                self._changed.pop(scope, None)

    def release_event(
        self,
        platform: Any,
        generation: Any,
        account_id: Any,
        route_kind: Any,
        peer_id: Any,
        message_id: Any,
    ) -> bool:
        """Allow a retry after an external event failed before Core accepted it."""

        scope = self._scope(platform, generation, account_id, route_kind, peer_id)
        event_id = _plain_id(message_id)
        if scope is None or event_id is None:
            return False
        return self._seen.pop((scope, event_id), None) is not None


outbound_echo_registry = OutboundEchoRegistry()


__all__ = [
    "EXTERNAL_SELF_MESSAGE_SUFFIX",
    "EXTERNAL_SELF_MESSAGE_MARKER",
    "sanitize_external_self_sender_name",
    "external_self_sender_name",
    "strip_external_self_message_marker",
    "build_external_self_message_event",
    "is_external_self_message_event",
    "is_external_self_message",
    "OutboundEchoRegistry",
    "outbound_echo_registry",
]
