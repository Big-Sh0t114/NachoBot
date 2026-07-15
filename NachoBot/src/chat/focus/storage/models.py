"""Persistence records which are separate from live coordinator objects."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import FocusEventStatus


@dataclass(frozen=True, slots=True)
class FocusGroupStateRecord:
    group_id: str
    active_chat_id: str | None
    epoch: int
    membership_hash: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class FocusCursorRecord:
    group_id: str
    chat_id: str
    processed_row_id: int
    last_viewed_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class FocusEventRecord:
    event_id: str
    group_id: str
    chat_id: str
    revision: int
    first_row_id: int
    last_row_id: int
    unread_count: int
    has_mention: bool
    has_at: bool
    latest_preview: str
    status: FocusEventStatus
    last_delivered_revision: int
    visible: bool
    created_at: float
    updated_at: float
    expires_at: float | None


@dataclass(frozen=True, slots=True)
class HandoffReservationRecord:
    reservation_id: str
    handoff_id: str
    cycle_id: str
    target_chat_id: str
    target_epoch: int
    state: str
    created_at: float
    lease_expires_at: float
    delivery_id: str | None
