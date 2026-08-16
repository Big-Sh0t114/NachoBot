"""Adapter-declared runtime behavior for a chat message.

Adapters publish this contract in ``BaseMessageInfo.additional_config`` under
``runtime_capabilities``.  Core code must use these capabilities instead of
inferring behavior from a platform name, group id, or template name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


RUNTIME_CAPABILITIES_KEY = "runtime_capabilities"
PLATFORM_EVENT_KEY = "platform_event"
SUPPORTED_SCHEMA_VERSION = 1

_TOOL_MODES = {"standard", "mcp_only", "disabled"}
_WEB_SEARCH_MODES = {"standard", "disabled"}
_REPLY_DELIVERY_MODES = {"chunked", "aggregate_tagged_text", "json_envelope"}
_PERSON_PROFILE_MODES = {"standard", "low_latency", "disabled"}
_TTS_LANGUAGES = {"", "ja", "zh"}
_IDENTITY_MODES = {"standard", "external"}


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Platform-neutral behavior requested by the active adapter."""

    schema_version: int = SUPPORTED_SCHEMA_VERSION
    planner_bypass: bool = False
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


def additional_config_from_message(message: Any) -> Mapping[str, Any]:
    if isinstance(message, Mapping):
        direct = message.get("additional_config")
        if isinstance(direct, Mapping) and direct:
            return direct
        additional_data = message.get("additional_data")
        if isinstance(additional_data, Mapping) and additional_data:
            return additional_data
        base_info = message.get("message_base_info")
        if isinstance(base_info, Mapping) and isinstance(base_info.get("additional_config"), Mapping):
            return base_info["additional_config"]
    direct = getattr(message, "additional_config", None)
    if isinstance(direct, Mapping) and direct:
        return direct
    additional_data = getattr(message, "additional_data", None)
    if isinstance(additional_data, Mapping) and additional_data:
        return additional_data
    base_info = getattr(message, "message_base_info", None)
    if isinstance(base_info, Mapping) and isinstance(base_info.get("additional_config"), Mapping):
        return base_info["additional_config"]
    message_info = getattr(message, "message_info", None)
    nested = getattr(message_info, "additional_config", None)
    return nested if isinstance(nested, Mapping) else {}


def runtime_capabilities_from_message(message: Any) -> RuntimeCapabilities:
    additional_config = additional_config_from_message(message)
    return RuntimeCapabilities.from_mapping(additional_config.get(RUNTIME_CAPABILITIES_KEY))


def runtime_capabilities_from_stream(chat_stream: Any) -> RuntimeCapabilities:
    context = getattr(chat_stream, "context", None)
    return runtime_capabilities_from_message(getattr(context, "message", None))


def platform_event_from_message(message: Any) -> PlatformEvent | None:
    additional_config = additional_config_from_message(message)
    return PlatformEvent.from_mapping(additional_config.get(PLATFORM_EVENT_KEY))


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
