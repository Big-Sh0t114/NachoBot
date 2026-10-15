"""Helpers for keeping a turn's explicit wake rows in bounded chat context."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


def merge_planner_context_messages(
    history_messages: Iterable[Any],
    trigger_messages: Iterable[Any] | None,
    *,
    chat_id: str,
    limit: int,
    history_before: float | None = None,
) -> list[Any]:
    """Merge a turn's wake snapshot into same-chat history and keep its newest rows.

    Database history is bounded by a wall-clock query, while the messages that woke
    the current turn are already an authoritative snapshot. The snapshot may
    therefore contain rows newer than ``history_before``. Identity aliases let a
    richer snapshot row replace its copy from history without duplicating it.
    """

    merged: list[tuple[Any, bool]] = []

    def add(message: Any, *, is_explicit: bool) -> None:
        if not _belongs_to_chat(message, chat_id):
            return
        index = _find_duplicate_index([entry[0] for entry in merged], message)
        if index is None:
            merged.append((message, is_explicit))
        elif is_explicit:
            merged[index] = (message, True)

    for message in history_messages:
        if history_before is not None:
            timestamp = _message_timestamp(message)
            if timestamp is None or timestamp >= history_before:
                continue
        add(message, is_explicit=False)

    for message in trigger_messages or ():
        add(message, is_explicit=True)

    # Python's stable sort retains source order for equal timestamps. Trigger rows
    # are appended after history rows, so a same-time wake row stays after its past.
    merged.sort(key=lambda entry: _message_timestamp(entry[0]) or 0.0)
    explicit_messages = [message for message, is_explicit in merged if is_explicit]
    ordered_messages = [message for message, _is_explicit in merged]

    try:
        configured_capacity = int(limit)
    except (TypeError, ValueError, OverflowError):
        configured_capacity = 1
    bounded_limit = max(1, configured_capacity)
    if len(ordered_messages) <= bounded_limit:
        return ordered_messages

    # Reserve capacity for this turn's explicit wake batch. If it is itself
    # larger than the context budget, retain its newest messages so the current
    # wake remains represented while the overall context stays bounded.
    if len(explicit_messages) >= bounded_limit:
        return explicit_messages[-bounded_limit:]
    explicit_object_ids = {id(message) for message in explicit_messages}
    history_candidates = [message for message in ordered_messages if id(message) not in explicit_object_ids]
    remaining = bounded_limit - len(explicit_messages)
    return sorted(
        [*history_candidates[-remaining:], *explicit_messages],
        key=lambda message: _message_timestamp(message) or 0.0,
    )


def _belongs_to_chat(message: Any, chat_id: str) -> bool:
    message_chat_id = getattr(message, "chat_id", None)
    if message_chat_id is None:
        chat_info = getattr(message, "chat_info", None)
        message_chat_id = getattr(chat_info, "stream_id", None)
    return not message_chat_id or str(message_chat_id) == str(chat_id)


def _row_identity(message: Any) -> str | None:
    for field in ("focus_row_id", "row_id", "id"):
        value = getattr(message, field, None)
        if value is not None and str(value) != "":
            return str(value)
    return None


def _message_id(message: Any) -> str | None:
    message_id = getattr(message, "message_id", None)
    if message_id is None or not str(message_id).strip() or str(message_id).strip().lower() == "notice":
        return None
    return str(message_id)


def _find_duplicate_index(messages: list[Any], candidate: Any) -> int | None:
    candidate_row = _row_identity(candidate)
    candidate_message_id = _message_id(candidate)
    message_id_matches: list[int] = []
    for index, existing in enumerate(messages):
        existing_row = _row_identity(existing)
        if candidate_row is not None and existing_row is not None:
            if candidate_row == existing_row:
                return index
            # Conflicting authoritative row IDs take precedence over a reused
            # legacy message ID.
            continue
        existing_message_id = _message_id(existing)
        if candidate_message_id is not None and candidate_message_id == existing_message_id:
            message_id_matches.append(index)
    # A message ID is only a fallback when it identifies one row unambiguously.
    return message_id_matches[0] if len(message_id_matches) == 1 else None


def _message_timestamp(message: Any) -> float | None:
    value = getattr(message, "time", None)
    if isinstance(value, bool) or value is None:
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if math.isfinite(timestamp) else None
