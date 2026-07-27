"""Versioned wire protocol for the NachoBot Live2D adapter.

NachoBot sends ``avatar.command`` envelopes to the adapter. The adapter emits
``avatar.interaction`` envelopes back to the caller. Platform-specific message
objects must never cross this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
from typing import Any, Mapping
from uuid import uuid4

PROTOCOL_VERSION = "1.0"
COMMAND_MESSAGE_TYPE = "avatar.command"
INTERACTION_MESSAGE_TYPE = "avatar.interaction"


class ProtocolError(ValueError):
    """Raised when an incoming protocol envelope is malformed."""


class AvatarEvent(StrEnum):
    """Canonical commands understood by every avatar implementation."""

    STATE = "state"
    SPEAKING = "speaking"
    EMOTION = "emotion"
    ACTION = "action"
    MOTION = "motion"
    RANDOM_MOTION = "random_motion"
    GAZE = "gaze"
    PARAM_TWEEN = "param_tween"
    PLAY_AUDIO = "play_audio"
    STOP_AUDIO = "stop_audio"
    SHUTDOWN = "shutdown"
    PING = "ping"


class AvatarInteraction(StrEnum):
    """Events produced by the adapter for NachoBot or another host."""

    READY = "ready"
    CLICK = "click"
    POKE = "poke"
    PONG = "pong"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AvatarCommand:
    event: AvatarEvent
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid4().hex)
    version: str = PROTOCOL_VERSION

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AvatarCommand":
        message_type = raw.get("type")
        if message_type != COMMAND_MESSAGE_TYPE:
            raise ProtocolError(
                f"unsupported message type: {message_type!r}; "
                f"expected {COMMAND_MESSAGE_TYPE!r}"
            )

        version = str(raw.get("version") or "")
        if version.split(".", 1)[0] != PROTOCOL_VERSION.split(".", 1)[0]:
            raise ProtocolError(
                f"incompatible protocol version: {version!r}; "
                f"supported major version is {PROTOCOL_VERSION.split('.', 1)[0]}"
            )

        request_id = str(raw.get("request_id") or uuid4().hex)

        try:
            event = AvatarEvent(str(raw.get("event") or ""))
        except ValueError as exc:
            raise ProtocolError(f"unsupported avatar event: {raw.get('event')!r}") from exc

        payload_raw = raw.get("payload", {})
        if payload_raw is None:
            payload_raw = {}
        if not isinstance(payload_raw, Mapping):
            raise ProtocolError("payload must be a JSON object")

        return cls(
            event=event,
            payload=dict(payload_raw),
            request_id=request_id,
            version=version or PROTOCOL_VERSION,
        )

    @classmethod
    def from_json(cls, raw_json: str) -> "AvatarCommand":
        try:
            raw = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"invalid JSON: {exc.msg}") from exc
        if not isinstance(raw, Mapping):
            raise ProtocolError("command envelope must be a JSON object")
        return cls.from_mapping(raw)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "type": COMMAND_MESSAGE_TYPE,
            "version": self.version,
            "request_id": self.request_id,
            "event": self.event.value,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_mapping(), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class InteractionEvent:
    event: AvatarInteraction
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    version: str = PROTOCOL_VERSION

    def to_mapping(self) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "type": INTERACTION_MESSAGE_TYPE,
            "version": self.version,
            "event": self.event.value,
            "payload": self.payload,
        }
        if self.request_id:
            envelope["request_id"] = self.request_id
        return envelope

    def to_json(self) -> str:
        return json.dumps(self.to_mapping(), ensure_ascii=False)


def error_event(message: str, request_id: str | None = None) -> InteractionEvent:
    return InteractionEvent(
        event=AvatarInteraction.ERROR,
        payload={"message": message},
        request_id=request_id,
    )
