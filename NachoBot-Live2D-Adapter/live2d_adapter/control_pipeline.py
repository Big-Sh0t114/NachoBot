"""Pure reply-control parsing and staged application for the Live2D adapter.

The Bilibili adapter deliberately does not know about emotion or action values.
This module is the single owner of the structured reply envelope and keeps the
short-lived control state scoped to the WebSocket client that prepared it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

ALLOWED_EMOTIONS = frozenset({"normal", "shy", "disgust", "angry"})

# Keep this table byte-for-byte compatible with the labels historically emitted
# by the Bilibili prompt.  IDLE and GENERAL are intentionally represented as
# ignored controls, just as they were before the split.
ACTION_TO_CANONICAL_ID: dict[str, str] = {
    "待机/放松": "IDLE",
    "点头/同意": "NOD",
    "摇头/否定": "SHAKE_HEAD",
    "转身向左/看左边": "TURN_LEFT",
    "转身向右/看右边": "TURN_RIGHT",
    "眨眼/卖萌/Wink": "WINK",
    "身体晃动/开心/兴奋": "HAPPY",
    "歪头/疑惑/思考": "TILT_HEAD",
    "害羞/移开视线/不好意思": "LOOK_AWAY",
    "一般": "GENERAL",
}

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "y", "on"})
_FENCED_JSON_RE = re.compile(
    r"^\s*```(?:json)?\s*(\{.*?\})\s*```\s*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class PreparedReply:
    """The only reply shape exposed to a platform adapter."""

    reply: str
    web_search: bool
    search_query: str
    control_id: str

    def to_payload(self) -> dict[str, Any]:
        # Do not add emotion/action here: this payload crosses the platform
        # boundary and must remain opaque with respect to avatar controls.
        return {
            "reply": self.reply,
            "web_search": self.web_search,
            "search_query": self.search_query,
            "control_id": self.control_id,
        }


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """An explicit, correlated result for an apply request."""

    control_id: str
    status: str
    applied: bool = False
    already_applied: bool = False
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.applied or self.already_applied

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "control_id": self.control_id,
            "status": self.status,
            "applied": self.applied,
            "already_applied": self.already_applied,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True, slots=True)
class _StagedControl:
    control_id: str
    reply: str
    web_search: bool
    search_query: str
    emotion: str | None
    action_id: str | None
    created_at: float


class ControlPipeline:
    """Parse, validate, stage, and apply reply controls exactly once.

    The class is intentionally independent from the renderer and from any
    platform.  ``apply`` invokes the supplied synchronous callback only for the
    first successful application, which lets the runtime decide how a canonical
    action maps to a particular model.
    """

    DEFAULT_TTL_SECONDS = 120.0
    DEFAULT_MAX_CONTROLS = 256
    DEFAULT_CLIENT_ID = "default"

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_controls: int = DEFAULT_MAX_CONTROLS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_controls <= 0:
            raise ValueError("max_controls must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self.max_controls = int(max_controls)
        self._clock = clock or time.monotonic
        self._pending: dict[str, OrderedDict[str, _StagedControl]] = {}
        self._applied: dict[str, OrderedDict[str, float]] = {}
        self._lock = threading.RLock()

    def prepare(
        self,
        raw_reply: Any,
        control_id: str,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
    ) -> PreparedReply:
        """Normalize a reply and stage its private avatar controls."""

        normalized_id = self._normalize_id(control_id)
        scope = self._normalize_client_id(client_id)
        reply, web_search, search_query, emotion, action_id = self._parse(raw_reply)
        prepared = PreparedReply(
            reply=reply,
            web_search=web_search,
            search_query=search_query,
            control_id=normalized_id,
        )
        staged = _StagedControl(
            control_id=normalized_id,
            reply=reply,
            web_search=web_search,
            search_query=search_query,
            emotion=emotion,
            action_id=action_id,
            created_at=self._clock(),
        )
        with self._lock:
            self._purge_locked(scope)
            pending = self._pending.setdefault(scope, OrderedDict())
            applied = self._applied.setdefault(scope, OrderedDict())
            # A duplicate preparation request is safe to replace while it is
            # still pending.  Once applied, keep the applied marker and stage
            # no second control for that request id.
            if normalized_id not in applied:
                pending[normalized_id] = staged
            self._trim_locked(scope)
        return prepared

    # Explicit alias used by runtime-facing callers and tests.
    prepare_reply = prepare

    def apply(
        self,
        control_id: str,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
        apply_callback: Callable[[_StagedControl], None] | None = None,
    ) -> ApplyOutcome:
        """Apply one staged control, returning an idempotent outcome."""

        normalized_id = self._normalize_id(control_id)
        scope = self._normalize_client_id(client_id)
        with self._lock:
            self._purge_locked(scope)
            applied = self._applied.setdefault(scope, OrderedDict())
            if normalized_id in applied:
                applied.move_to_end(normalized_id)
                return ApplyOutcome(
                    control_id=normalized_id,
                    status="already_applied",
                    already_applied=True,
                )
            pending = self._pending.setdefault(scope, OrderedDict())
            staged = pending.get(normalized_id)
            if staged is None:
                return ApplyOutcome(
                    control_id=normalized_id,
                    status="unknown",
                    reason="unknown_or_expired_control_id",
                )

            # The callback is synchronous in the runtime.  Marking the request
            # only after it succeeds avoids claiming an application that did not
            # enqueue.  The lock also serializes concurrent retries.
            if apply_callback is not None:
                apply_callback(staged)
            pending.pop(normalized_id, None)
            applied[normalized_id] = self._clock()
            self._trim_locked(scope)
            return ApplyOutcome(
                control_id=normalized_id,
                status="applied",
                applied=True,
            )

    def discard_client(self, client_id: str) -> None:
        """Drop all staged and applied controls for a disconnected client."""

        scope = self._normalize_client_id(client_id)
        with self._lock:
            self._pending.pop(scope, None)
            self._applied.pop(scope, None)

    def clear(self) -> None:
        """Drop every staged and applied control across all client scopes."""

        with self._lock:
            self._pending.clear()
            self._applied.clear()

    def purge_expired(self) -> None:
        with self._lock:
            for scope in tuple(set(self._pending) | set(self._applied)):
                self._purge_locked(scope)

    def pending_count(self, client_id: str = DEFAULT_CLIENT_ID) -> int:
        with self._lock:
            scope = self._normalize_client_id(client_id)
            self._purge_locked(scope)
            return len(self._pending.get(scope, ()))

    def applied_count(self, client_id: str = DEFAULT_CLIENT_ID) -> int:
        with self._lock:
            scope = self._normalize_client_id(client_id)
            self._purge_locked(scope)
            return len(self._applied.get(scope, ()))

    @staticmethod
    def _normalize_id(value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("control_id is required")
        return normalized

    @staticmethod
    def _normalize_client_id(value: Any) -> str:
        return (
            str(value or ControlPipeline.DEFAULT_CLIENT_ID).strip()
            or ControlPipeline.DEFAULT_CLIENT_ID
        )

    def _purge_locked(self, scope: str) -> None:
        now = self._clock()
        cutoff = now - self.ttl_seconds
        pending = self._pending.get(scope)
        if pending is not None:
            for control_id, staged in tuple(pending.items()):
                if staged.created_at < cutoff:
                    pending.pop(control_id, None)
            if not pending:
                self._pending.pop(scope, None)
        applied = self._applied.get(scope)
        if applied is not None:
            for control_id, applied_at in tuple(applied.items()):
                if applied_at < cutoff:
                    applied.pop(control_id, None)
            if not applied:
                self._applied.pop(scope, None)

    def _trim_locked(self, scope: str) -> None:
        pending = self._pending.get(scope)
        applied = self._applied.get(scope)
        if pending is None or applied is None:
            return
        # Bound the combined per-client state.  Applied markers are retained
        # while newer pending controls arrive whenever possible, but neither
        # store can make the other grow beyond the configured total.
        while len(pending) + len(applied) > self.max_controls:
            oldest_pending = next(iter(pending.items()), None)
            oldest_applied = next(iter(applied.items()), None)
            if oldest_pending is None:
                applied.popitem(last=False)
            elif oldest_applied is None:
                pending.popitem(last=False)
            elif oldest_pending[1].created_at <= oldest_applied[1]:
                pending.popitem(last=False)
            else:
                applied.popitem(last=False)

    @classmethod
    def _parse(
        cls,
        raw_reply: Any,
    ) -> tuple[str, bool, str, str | None, str | None]:
        text = str(raw_reply or "").strip()
        data = cls._extract_json_object(text)
        if not isinstance(data, Mapping):
            return text, False, "", None, None

        reply_value = data.get("reply")
        reply = str(reply_value or "").strip() if reply_value is not None else text
        if not reply and reply_value is None:
            reply = text

        web_search = cls._truthy(data.get("web_search", False))
        query_value = data.get("search_query", "")
        search_query = str(query_value or "").strip() if web_search else ""

        emotion_value = data.get("emotion")
        emotion = str(emotion_value).strip().casefold() if emotion_value is not None else None
        if emotion not in ALLOWED_EMOTIONS:
            emotion = None

        action_value = data.get("action")
        action_label = str(action_value).strip() if action_value is not None else ""
        action_id = ACTION_TO_CANONICAL_ID.get(action_label)
        if action_id in {"IDLE", "GENERAL"}:
            action_id = None
        return reply, web_search, search_query, emotion, action_id

    @staticmethod
    def _extract_json_object(text: str) -> Mapping[str, Any] | None:
        if not text:
            return None
        fenced = _FENCED_JSON_RE.match(text)
        candidate = fenced.group(1) if fenced else None
        if candidate is None:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                return None
            candidate = text[start : end + 1]
        try:
            value = json.loads(candidate, strict=False)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, Mapping) else None

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() in _TRUTHY_VALUES
        return bool(value)


__all__ = [
    "ACTION_TO_CANONICAL_ID",
    "ALLOWED_EMOTIONS",
    "ApplyOutcome",
    "ControlPipeline",
    "PreparedReply",
]
