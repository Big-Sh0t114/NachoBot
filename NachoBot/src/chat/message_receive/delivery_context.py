"""Bind one message's opaque delivery target to asynchronous command work."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from src.chat.runtime_capabilities import additional_config_from_message


DELIVERY_TARGET_KEY = "delivery_target"


@dataclass(frozen=True, slots=True)
class DeliveryTargetScope:
    source_stream_id: str
    target_additional_config: dict[str, Any] | None


_ACTIVE_DELIVERY_TARGET: ContextVar[DeliveryTargetScope | None] = ContextVar(
    "nachobot_active_delivery_target", default=None
)


def delivery_target_additional_config(source: Any) -> dict[str, Any] | None:
    """Return only a deep-copied opaque reply target from one source message."""

    target = additional_config_from_message(source).get(DELIVERY_TARGET_KEY)
    if not isinstance(target, dict):
        return None
    return {DELIVERY_TARGET_KEY: copy.deepcopy(target)}


@contextmanager
def scoped_delivery_target(source_message: Any) -> Iterator[None]:
    """Snapshot and bind a command's source target until its execution settles."""

    source_stream = getattr(source_message, "chat_stream", None)
    scope = DeliveryTargetScope(
        source_stream_id=str(getattr(source_stream, "stream_id", "") or ""),
        target_additional_config=delivery_target_additional_config(source_message),
    )
    token = _ACTIVE_DELIVERY_TARGET.set(scope)
    try:
        yield
    finally:
        _ACTIVE_DELIVERY_TARGET.reset(token)


def current_delivery_target_binding(
    stream_id: str,
) -> tuple[bool, dict[str, Any] | None]:
    """Return whether a command scope is active and its stream-matched target copy.

    The active bit is separate from the optional target.  This lets callers
    distinguish legacy sends (which may use the latest stream context) from a
    scoped send that has no target or is aimed at a different stream.
    """

    scope = _ACTIVE_DELIVERY_TARGET.get()
    if scope is None:
        return False, None
    if scope.source_stream_id != str(stream_id):
        return True, None
    if scope.target_additional_config is None:
        return True, None
    return True, copy.deepcopy(scope.target_additional_config)
