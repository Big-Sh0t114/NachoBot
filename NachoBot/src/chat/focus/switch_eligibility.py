"""Synchronous eligibility check shared by Focus action parsers."""

from __future__ import annotations

from .coordinator import FocusCoordinator
from .models import ChatKind
from .switch_planner import has_active_focus_lease


def can_offer_switch_chat(coordinator: FocusCoordinator, chat_id: str) -> bool:
    """Allow group transfers and event-backed private safe returns."""

    if not has_active_focus_lease(chat_id):
        return False
    definition = coordinator.definition_for_chat(chat_id)
    if definition is None:
        return False
    source = coordinator.policy.member(definition, chat_id)
    if source is None:
        return False
    if source.kind is ChatKind.GROUP:
        return True
    return coordinator.has_safe_return_event(chat_id)


__all__ = ["can_offer_switch_chat"]
