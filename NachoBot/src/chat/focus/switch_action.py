"""Planner-facing normalization and server-authoritative Focus switching."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from src.chat.utils.chat_message_builder import get_raw_msg_before_timestamp_with_chat
from src.config.config import global_config

from .coordinator import FocusCoordinator
from .handoff_builder import HandoffBuilder, HandoffLimits
from .models import (
    ChatKind,
    FocusLease,
    HandoffPayload,
    StaleFocusLeaseError,
    SwitchChatRequest,
    SwitchResult,
)


SWITCH_CHAT_ACTION = "switch_chat"
_HANDOFF_FIELDS = ("task_summary", "known_facts", "pending_items", "recent_results")
_RECENT_SOURCE_MESSAGE_LIMIT = 10
_HANDOFF_PRESENT_KEY = "_focus_handoff_present"
_HANDOFF_MAPPING_KEY = "_focus_handoff_is_mapping"
_HANDOFF_NONEMPTY_KEY = "_focus_handoff_nonempty"


class SwitchDisposition(str, Enum):
    """Turn-control disposition after a switch attempt.

    ``SUCCESS`` describes the switch itself; ``RETRY`` and ``DROP`` describe
    what the current Focus turn should do when the switch was rejected.  A
    dropped turn still commits the observed event revision, but never claims
    that the switch succeeded.
    """

    SUCCESS = "success"
    RETRY = "retry"
    DROP = "drop"


_RETRYABLE_SWITCH_REASON_PREFIXES = (
    "cannot resolve Focus events",
    "target runtime preparation failed",
    "handoff persistence failed",
    "switch persistence failed",
    "switch compare-and-set failed",
    "Focus event revision changed",
    "Focus switch cooldown is active",
    "Focus group is transitioning",
)


def classify_switch_failure_reason(reason: str) -> SwitchDisposition:
    """Classify a failed switch reason without changing switch success state."""

    if reason.startswith(_RETRYABLE_SWITCH_REASON_PREFIXES):
        return SwitchDisposition.RETRY
    return SwitchDisposition.DROP


def classify_switch_result(result: SwitchResult) -> SwitchDisposition:
    """Classify a switch result for Focus turn control.

    Policy, malformed-input, stale-lease, and no-longer-pending failures are
    intentionally drop dispositions.  Only known, recoverable coordination
    failures requeue the turn; unknown failures fail closed and are consumed
    rather than creating an unbounded retry loop.
    """

    if result.success:
        return SwitchDisposition.SUCCESS
    return classify_switch_failure_reason(result.reason)


def _handoff_input_is_invalid_for_private_source(action_data: Mapping[str, Any]) -> bool:
    """Reject every supplied or malformed handoff on a private source.

    Planner normalization keeps shape metadata because a malformed value can
    otherwise be reduced to ``{}`` before the trusted switch boundary.
    """

    if action_data.get(_HANDOFF_PRESENT_KEY) is True:
        if not action_data.get(_HANDOFF_MAPPING_KEY) or action_data.get(_HANDOFF_NONEMPTY_KEY):
            return True

    if "handoff" not in action_data:
        return False
    raw_handoff = action_data.get("handoff")
    if not isinstance(raw_handoff, Mapping):
        return True
    return bool(raw_handoff)


def normalize_switch_action_data(action_json: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the model fields that are safe to carry into execution.

    In particular, target chat IDs, event revisions, epochs, policy versions and
    parent handoff IDs are never accepted from the planner.
    """

    event_id = action_json.get("event_id")
    normalized: dict[str, Any] = {"event_id": event_id.strip() if isinstance(event_id, str) else ""}

    if "handoff" not in action_json:
        return normalized

    raw_handoff = action_json.get("handoff")
    normalized[_HANDOFF_PRESENT_KEY] = True
    normalized[_HANDOFF_MAPPING_KEY] = isinstance(raw_handoff, Mapping)
    normalized[_HANDOFF_NONEMPTY_KEY] = bool(raw_handoff) if isinstance(raw_handoff, Mapping) else True
    if not isinstance(raw_handoff, Mapping):
        normalized["handoff"] = {}
        return normalized

    handoff: dict[str, Any] = {}
    task_summary = raw_handoff.get("task_summary")
    if isinstance(task_summary, str):
        handoff["task_summary"] = task_summary
    for field_name in _HANDOFF_FIELDS[1:]:
        values = raw_handoff.get(field_name)
        if isinstance(values, str):
            handoff[field_name] = [values]
        elif isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            handoff[field_name] = [value for value in values if isinstance(value, str)]
    normalized["handoff"] = handoff
    return normalized


def _payload_from_action_data(action_data: Mapping[str, Any]) -> HandoffPayload:
    raw = action_data.get("handoff")
    if not isinstance(raw, Mapping):
        raw = {}

    def strings(field_name: str) -> tuple[str, ...]:
        values = raw.get(field_name)
        if isinstance(values, str):
            return (values,)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            return ()
        return tuple(value for value in values if isinstance(value, str))

    summary = raw.get("task_summary")
    return HandoffPayload(
        task_summary=summary if isinstance(summary, str) else "",
        known_facts=strings("known_facts"),
        pending_items=strings("pending_items"),
        recent_results=strings("recent_results"),
    )


def format_recent_source_messages(
    messages: Sequence[Any],
    *,
    limit: int = _RECENT_SOURCE_MESSAGE_LIMIT,
) -> tuple[str, ...]:
    """Format bounded source-chat history for an untrusted Focus handoff."""

    if limit <= 0:
        return ()
    results: list[str] = []
    for message in messages[-limit:]:
        value = getattr(message, "processed_plain_text", None) or getattr(message, "display_message", None) or ""
        value = " ".join(str(value).replace("\x00", "").split())
        if not value:
            continue
        if len(value) > 280:
            value = value[:279].rstrip() + "…"
        user_info = getattr(message, "user_info", None)
        speaker = (
            getattr(user_info, "user_cardname", None)
            or getattr(user_info, "user_nickname", None)
            or getattr(user_info, "user_id", None)
            or "群成员"
        )
        speaker = " ".join(str(speaker).split())[:48] or "群成员"
        results.append(f"{speaker}: {value}")
    return tuple(results)


def _load_recent_source_results(chat_id: str) -> tuple[str, ...]:
    """Load source history at the trusted switch boundary.

    History is supplemental context: a storage failure must not invalidate an
    otherwise authorized switch.
    """

    try:
        messages = get_raw_msg_before_timestamp_with_chat(
            chat_id=chat_id,
            timestamp=time.time(),
            limit=_RECENT_SOURCE_MESSAGE_LIMIT,
        )
    except Exception:
        return ()
    return format_recent_source_messages(messages)


def _merge_source_history(
    payload: HandoffPayload,
    source_results: Sequence[str],
    *,
    source_display_name: str,
    target_display_name: str,
) -> HandoffPayload:
    recent_results = tuple(dict.fromkeys((*source_results, *payload.recent_results)))[:_RECENT_SOURCE_MESSAGE_LIMIT]
    return HandoffPayload(
        task_summary=payload.task_summary,
        source_display_name=source_display_name,
        target_display_name=target_display_name,
        known_facts=payload.known_facts,
        pending_items=payload.pending_items,
        recent_results=recent_results,
        excerpts=payload.excerpts,
    )


async def execute_switch_chat(
    coordinator: FocusCoordinator,
    *,
    lease: FocusLease,
    action_data: Mapping[str, Any],
    reasoning: str = "",
) -> SwitchResult:
    """Resolve and commit one terminal switch from server-issued Focus state."""

    if getattr(global_config.focus, "mode", "off") != "active":
        return SwitchResult(False, "Focus switch is disabled outside active mode", lease)

    definition = coordinator.definition_for_chat(lease.chat_id)
    if definition is None or definition.group_id != lease.group_id:
        return SwitchResult(False, "source chat is not in the bound Focus group", lease)

    source = coordinator.policy.member(definition, lease.chat_id)
    if source is None:
        return SwitchResult(False, "Focus switch source is not an enrolled member", lease)

    event_id = action_data.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        return SwitchResult(False, "switch_chat requires a server-issued event_id", lease)
    event_id = event_id.strip()

    try:
        events = await coordinator.events_for(lease)
    except StaleFocusLeaseError as exc:
        return SwitchResult(False, f"stale source lease: {exc}", lease)
    except Exception as exc:
        return SwitchResult(False, f"cannot resolve Focus events: {exc}", lease)
    event = next((item for item in events if item.event_id == event_id), None)
    if event is None:
        return SwitchResult(False, "Focus event is no longer pending for this turn", lease)

    target = coordinator.policy.member(definition, event.target_chat_id)
    if target is None:
        return SwitchResult(False, "Focus event target is outside the configured group", lease)
    metadata_only = coordinator.policy.can_switch_without_handoff(
        definition,
        lease.chat_id,
        event.target_chat_id,
    )
    if source.kind is ChatKind.PRIVATE and not metadata_only:
        return SwitchResult(False, "private-source Focus switch target is not an enrolled group or private chat", lease)
    if source.kind is ChatKind.PRIVATE and _handoff_input_is_invalid_for_private_source(action_data):
        return SwitchResult(False, "private-source metadata-only switch must not include a handoff", lease)
    if source.kind is ChatKind.GROUP and target.kind is ChatKind.PRIVATE and not getattr(
        global_config.focus, "allow_group_to_private", False
    ):
        return SwitchResult(False, "group-to-private Focus switching is disabled by configuration", lease)

    decision = coordinator.policy.decide_switch(
        definition,
        lease.chat_id,
        event.target_chat_id,
        has_handoff=not metadata_only,
    )
    if not decision.allowed:
        return SwitchResult(False, decision.reason, lease)

    if metadata_only:
        request = SwitchChatRequest(
            lease=lease,
            event_id=event.event_id,
            expected_event_revision=event.revision,
            reasoning=reasoning,
        )
        return await coordinator.switch_chat(request, None)

    parent = None
    try:
        active_handoffs = await coordinator.handoff_store.get_active(
            lease.group_id,
            lease.chat_id,
            lease.epoch,
        )
        if active_handoffs:
            parent = active_handoffs[-1]
    except Exception:
        # Parent inheritance is useful but must never weaken switch validation.
        parent = None

    payload = _merge_source_history(
        _payload_from_action_data(action_data),
        _load_recent_source_results(lease.chat_id),
        source_display_name=source.display_name or source.chat_id,
        target_display_name=target.display_name or target.chat_id,
    )
    focus_config = global_config.focus
    builder = HandoffBuilder(
        HandoffLimits(
            ttl_seconds=focus_config.handoff_ttl_seconds,
            max_successful_cycles=focus_config.handoff_successful_cycles,
            prompt_token_budget=focus_config.handoff_prompt_tokens,
        )
    )
    handoff = builder.build(
        group_id=lease.group_id,
        source_chat_id=lease.chat_id,
        target_chat_id=event.target_chat_id,
        source_epoch=lease.epoch,
        policy_version=coordinator.policy.version,
        payload=payload,
        parent=parent,
    )
    request = SwitchChatRequest(
        lease=lease,
        event_id=event.event_id,
        expected_event_revision=event.revision,
        reasoning=reasoning,
    )
    return await coordinator.switch_chat(request, handoff)


__all__ = [
    "SwitchDisposition",
    "SWITCH_CHAT_ACTION",
    "classify_switch_failure_reason",
    "classify_switch_result",
    "execute_switch_chat",
    "format_recent_source_messages",
    "normalize_switch_action_data",
]
