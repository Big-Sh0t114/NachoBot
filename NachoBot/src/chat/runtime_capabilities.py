"""Adapter-declared runtime behavior for a chat message.

Adapters publish this contract in ``BaseMessageInfo.additional_config`` under
``runtime_capabilities``.  Core code must use these capabilities instead of
inferring behavior from a platform name, group id, or template name.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping


RUNTIME_CAPABILITIES_KEY = "runtime_capabilities"
PLATFORM_EVENT_KEY = "platform_event"
SUPPORTED_SCHEMA_VERSION = 1

_TOOL_MODES = {"standard", "mcp_only", "disabled"}
_WEB_SEARCH_MODES = {"standard", "disabled"}
_REPLY_DELIVERY_MODES = {"chunked", "aggregate_tagged_text", "json_envelope", "tts_text"}
_PERSON_PROFILE_MODES = {"standard", "low_latency", "disabled"}
_TTS_LANGUAGES = {"", "ja", "zh"}
_IDENTITY_MODES = {"standard", "external"}
_SCOPED_CAPABILITIES: ContextVar[tuple[str, "RuntimeCapabilities"] | None] = ContextVar(
    "nachobot_scoped_runtime_capabilities", default=None
)


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Platform-neutral behavior requested by the active adapter."""

    schema_version: int = SUPPORTED_SCHEMA_VERSION
    planner_bypass: bool = False
    planner_bypass_declared: bool = False
    history_summarization: bool = True
    notice_actions: bool = True
    relation_inference: bool = True
    expression_selection: bool = True
    memory_retrieval: bool = True
    mid_term_memory: bool = True
    knowledge_retrieval: bool = True
    reply_model_group: str = ""
    tool_mode: str = "standard"
    web_search_mode: str = "standard"
    reply_delivery: str = "chunked"
    person_profile_mode: str = "standard"
    person_profile_timeout_seconds: float = 0.5
    typo_enabled: bool = True
    tts_language: str = ""
    identity_mode: str = "standard"
    voice_stream: bool = False
    reply_controls: bool = False
    control_emotions: tuple[str, ...] = ()
    control_actions: tuple[str, ...] = ()
    interruption_feedback_count: int = 0

    @classmethod
    def from_mapping(cls, value: Any) -> "RuntimeCapabilities":
        if not isinstance(value, Mapping):
            return cls()

        try:
            schema_version = int(value.get("schema_version", SUPPORTED_SCHEMA_VERSION))
        except (TypeError, ValueError):
            return cls()
        if schema_version != SUPPORTED_SCHEMA_VERSION:
            return cls()

        return cls(
            schema_version=schema_version,
            planner_bypass=_bool(value, "planner_bypass", False),
            planner_bypass_declared=isinstance(value.get("planner_bypass"), bool),
            history_summarization=_bool(value, "history_summarization", True),
            notice_actions=_bool(value, "notice_actions", True),
            relation_inference=_bool(value, "relation_inference", True),
            expression_selection=_bool(value, "expression_selection", True),
            memory_retrieval=_bool(value, "memory_retrieval", True),
            mid_term_memory=_bool(value, "mid_term_memory", True),
            knowledge_retrieval=_bool(value, "knowledge_retrieval", True),
            reply_model_group=_text(value.get("reply_model_group")),
            tool_mode=_choice(value.get("tool_mode"), _TOOL_MODES, "standard"),
            web_search_mode=_choice(value.get("web_search_mode"), _WEB_SEARCH_MODES, "standard"),
            reply_delivery=_choice(value.get("reply_delivery"), _REPLY_DELIVERY_MODES, "chunked"),
            person_profile_mode=_choice(
                value.get("person_profile_mode"),
                _PERSON_PROFILE_MODES,
                "standard",
            ),
            person_profile_timeout_seconds=_positive_float(
                value.get("person_profile_timeout_seconds"),
                0.5,
            ),
            typo_enabled=_bool(value, "typo_enabled", True),
            tts_language=_choice(value.get("tts_language"), _TTS_LANGUAGES, ""),
            identity_mode=_choice(value.get("identity_mode"), _IDENTITY_MODES, "standard"),
            voice_stream=_bool(value, "voice_stream", False),
            reply_controls=_bool(value, "reply_controls", False),
            control_emotions=_choices(value.get("control_emotions")),
            control_actions=_choices(value.get("control_actions")),
            interruption_feedback_count=_interruption_feedback_count(value.get("interruption_feedback")),
        )


@dataclass(frozen=True, slots=True)
class PlatformEvent:
    """A normalized adapter event that affects a person's support state."""

    kind: str
    amount: float = 0.0
    membership_days: int = 0

    @classmethod
    def from_mapping(cls, value: Any) -> "PlatformEvent | None":
        if not isinstance(value, Mapping):
            return None
        kind = _text(value.get("kind")).lower()
        if kind not in {"support", "membership"}:
            return None
        try:
            amount = max(0.0, float(value.get("amount", 0.0)))
        except (TypeError, ValueError):
            amount = 0.0
        try:
            membership_days = max(0, int(value.get("membership_days", 0)))
        except (TypeError, ValueError):
            membership_days = 0
        return cls(kind=kind, amount=amount, membership_days=membership_days)


@dataclass(frozen=True, slots=True)
class RuntimeMessageBatch:
    """Message routing derived from the exact unread batch being handled."""

    direct_messages: tuple[Any, ...] = ()
    planner_messages: tuple[Any, ...] = ()
    explicit_planner_messages: tuple[Any, ...] = ()

    @property
    def requires_planner(self) -> bool:
        return bool(self.planner_messages)

    @property
    def planner_bypass(self) -> bool:
        return bool(self.direct_messages) and not self.planner_messages


def additional_config_from_message(message: Any) -> Mapping[str, Any]:
    def _as_mapping(value: Any) -> Mapping[str, Any] | None:
        if isinstance(value, Mapping):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            return parsed if isinstance(parsed, Mapping) else None
        return None

    if isinstance(message, Mapping):
        direct = _as_mapping(message.get("additional_config"))
        if direct:
            return direct
        additional_data = message.get("additional_data")
        if isinstance(additional_data, Mapping) and additional_data:
            return additional_data
        base_info = message.get("message_base_info")
        if isinstance(base_info, Mapping):
            nested = _as_mapping(base_info.get("additional_config"))
            if nested is not None:
                return nested
    direct = _as_mapping(getattr(message, "additional_config", None))
    if direct:
        return direct
    additional_data = getattr(message, "additional_data", None)
    if isinstance(additional_data, Mapping) and additional_data:
        return additional_data
    base_info = getattr(message, "message_base_info", None)
    if isinstance(base_info, Mapping):
        nested = _as_mapping(base_info.get("additional_config"))
        if nested is not None:
            return nested
    message_info = getattr(message, "message_info", None)
    nested = _as_mapping(getattr(message_info, "additional_config", None))
    return nested if nested is not None else {}


def runtime_capabilities_from_message(message: Any) -> RuntimeCapabilities:
    additional_config = additional_config_from_message(message)
    return RuntimeCapabilities.from_mapping(additional_config.get(RUNTIME_CAPABILITIES_KEY))


def runtime_capabilities_from_stream(chat_stream: Any) -> RuntimeCapabilities:
    scoped = _SCOPED_CAPABILITIES.get()
    if scoped is not None and str(getattr(chat_stream, "stream_id", "")) == scoped[0]:
        return scoped[1]
    context = getattr(chat_stream, "context", None)
    return runtime_capabilities_from_message(getattr(context, "message", None))


@contextmanager
def scoped_runtime_capabilities(
    stream_id: str,
    capabilities: RuntimeCapabilities,
) -> Iterator[None]:
    """Bind an immutable trigger-message snapshot to this async generation task."""
    token = _SCOPED_CAPABILITIES.set((str(stream_id), capabilities))
    try:
        yield
    finally:
        _SCOPED_CAPABILITIES.reset(token)


def platform_event_from_message(message: Any) -> PlatformEvent | None:
    additional_config = additional_config_from_message(message)
    return PlatformEvent.from_mapping(additional_config.get(PLATFORM_EVENT_KEY))


def classify_runtime_message_batch(
    messages: Any,
    *,
    bot_user_id: str = "",
    is_system_event: Callable[[Any], Any] | None = None,
) -> RuntimeMessageBatch:
    """Split one unread batch using each message's declared capability snapshot."""

    direct_messages: list[Any] = []
    planner_messages: list[Any] = []
    explicit_planner_messages: list[Any] = []
    for message in messages or ():
        user_info = _message_value(message, "user_info")
        user_id = _message_value(user_info, "user_id") if user_info is not None else None
        if user_id and bot_user_id and str(user_id) == str(bot_user_id):
            continue

        capabilities = runtime_capabilities_from_message(message)
        additional_config = additional_config_from_message(message)
        has_raw_system_event = "system_event" in additional_config
        valid_event = bool(is_system_event(message)) if is_system_event is not None else False
        valid_event = valid_event or platform_event_from_message(message) is not None
        malformed_event = has_raw_system_event and not valid_event
        has_sender = bool(user_id)
        has_direct_target = (has_sender and not malformed_event) or (not has_sender and valid_event)

        if capabilities.planner_bypass and has_direct_target:
            direct_messages.append(message)
        else:
            planner_messages.append(message)
            if capabilities.planner_bypass_declared and not capabilities.planner_bypass:
                explicit_planner_messages.append(message)

    return RuntimeMessageBatch(
        direct_messages=tuple(direct_messages),
        planner_messages=tuple(planner_messages),
        explicit_planner_messages=tuple(explicit_planner_messages),
    )


def planner_target_eligible(message: Any) -> bool:
    """Keep direct-reply messages in context but out of Planner target maps."""

    return not runtime_capabilities_from_message(message).planner_bypass


def _message_value(message: Any, key: str) -> Any:
    return message.get(key) if isinstance(message, Mapping) else getattr(message, key, None)


def _bool(value: Mapping[str, Any], key: str, default: bool) -> bool:
    candidate = value.get(key, default)
    return candidate if isinstance(candidate, bool) else default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _choice(value: Any, choices: set[str], default: str) -> str:
    normalized = _text(value).lower()
    return normalized if normalized in choices else default


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _interruption_feedback_count(value: Any) -> int:
    if not isinstance(value, Mapping):
        return 0
    count = value.get("count")
    if type(count) is not int:
        return 0
    return max(0, min(count, 8))


def _choices(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    output: list[str] = []
    for item in value[:32]:
        candidate = str(item or "").strip()
        if candidate and len(candidate) <= 64 and candidate not in output:
            output.append(candidate)
    return tuple(output)
