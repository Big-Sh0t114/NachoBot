"""Typed domain models for Focus cross-chat orchestration.

The objects in this module deliberately contain no Heartflow, Replyer, or
adapter types.  They form the boundary between those systems and the Focus
coordinator, which keeps integration code explicit and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum, IntFlag, auto
from typing import Mapping


class ChatKind(str, Enum):
    GROUP = "group"
    PRIVATE = "private"


class FocusGroupPhase(str, Enum):
    RUNNING = "running"
    TRANSITIONING = "transitioning"
    STOPPING = "stopping"
    STOPPED = "stopped"


class WakeReason(IntFlag):
    NONE = 0
    LOCAL_MESSAGE = auto()
    FOCUS_EVENT = auto()
    SWITCH_TARGET = auto()
    TIMER = auto()
    RETRY = auto()
    STOP = auto()


class TurnStatus(str, Enum):
    COMPLETED = "completed"
    NOOP = "noop"
    SWITCHED = "switched"
    STALE = "stale"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DROPPED_BY_POLICY = "dropped_by_policy"


class EffectKind(str, Enum):
    SEND = "send"
    ACTION = "action"


class FocusEventStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class HandoffStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class HandoffKind(str, Enum):
    """Persisted handoff meaning.

    The kind is deliberately explicit instead of being inferred from a private
    source and an empty payload.  Legacy rows without a kind are treated as
    ordinary content by the storage adapter.
    """

    CONTENT_V1 = "content_v1"
    TRANSITION_IDENTITY_V1 = "transition_identity_v1"


@dataclass(frozen=True, slots=True)
class FocusMember:
    """A resolved chat stream explicitly enrolled in a Focus group."""

    chat_id: str
    kind: ChatKind
    display_name: str = ""
    allow_import: bool = True
    allow_export: bool = True
    platform: str = ""
    planner_bypass: bool = False

class FocusSessionPriority(IntEnum):
    """Deterministic preemption order for Focus-managed sessions."""

    NORMAL_GROUP = 1
    PRIVATE = 2
    PLANNER_BYPASS = 3


def focus_session_priority(member: FocusMember) -> FocusSessionPriority:
    if member.planner_bypass:
        return FocusSessionPriority.PLANNER_BYPASS
    if member.kind is ChatKind.PRIVATE:
        return FocusSessionPriority.PRIVATE
    return FocusSessionPriority.NORMAL_GROUP


@dataclass(frozen=True, slots=True)
class FocusGroupDefinition:
    group_id: str
    members: tuple[FocusMember, ...]
    initial_chat_id: str | None = None

    def __post_init__(self) -> None:
        if not self.group_id.strip():
            raise ValueError("Focus group_id cannot be empty")
        if len(self.members) < 2:
            raise ValueError(f"Focus group {self.group_id!r} requires at least two members")
        member_ids = [member.chat_id for member in self.members]
        if any(not chat_id for chat_id in member_ids):
            raise ValueError(f"Focus group {self.group_id!r} has an empty chat_id")
        if len(member_ids) != len(set(member_ids)):
            raise ValueError(f"Focus group {self.group_id!r} has duplicate chat members")
        if self.initial_chat_id is not None and self.initial_chat_id not in member_ids:
            raise ValueError(f"Initial chat {self.initial_chat_id!r} is not a member of {self.group_id!r}")


def trusted_member_display_name(member: FocusMember, *, role: str) -> str:
    """Return a server-owned label safe for a transition identity sentence.

    Private IDs are routing identifiers, not display names.  If an enrolled
    private member has no configured display name, use a generic role label so
    the identity handoff cannot disclose the raw private chat ID.
    """

    configured = " ".join(str(member.display_name or "").split())
    # Display names are configuration, but keeping angle brackets out of the
    # model block ensures a malformed configured label cannot create a tag.
    configured = configured.replace("<", "＜").replace(">", "＞")
    configured = configured[:160].rstrip()
    if configured:
        return configured
    if member.kind is ChatKind.PRIVATE:
        return "上一私聊" if role == "source" else "当前私聊"
    return "上一群聊" if role == "source" else "当前群聊"


def trusted_transition_labels(
    definition: FocusGroupDefinition,
    source_chat_id: str,
    target_chat_id: str,
) -> tuple[str, str]:
    """Resolve transition labels only from explicitly enrolled members."""

    source = next((member for member in definition.members if member.chat_id == source_chat_id), None)
    target = next((member for member in definition.members if member.chat_id == target_chat_id), None)
    if source is None or target is None:
        raise ValueError("Focus transition source and target must be enrolled members")
    return (
        trusted_member_display_name(source, role="source"),
        trusted_member_display_name(target, role="target"),
    )


@dataclass(frozen=True, slots=True)
class StoredMessageRef:
    """Stable reference returned only after a message has been stored."""

    row_id: int
    chat_id: str
    message_id: str
    message_time: float

    def __post_init__(self) -> None:
        if self.row_id <= 0:
            raise ValueError("Stored message row_id must be positive")


@dataclass(frozen=True, slots=True)
class FocusLease:
    group_id: str
    chat_id: str
    epoch: int
    turn_id: str


@dataclass(frozen=True, slots=True)
class FocusEventSnapshot:
    event_id: str
    revision: int
    target_chat_id: str
    display_name: str
    unread_count: int
    first_unread: StoredMessageRef
    last_unread: StoredMessageRef
    is_mentioned: bool = False
    is_at: bool = False
    latest_preview: str = ""


@dataclass(frozen=True, slots=True)
class RestoredFocusEvent:
    """Durable pending event reconstructed before the coordinator starts."""

    snapshot: FocusEventSnapshot
    last_delivered_revision: int = 0
    visible: bool = False

    def __post_init__(self) -> None:
        if self.last_delivered_revision < 0:
            raise ValueError("Focus delivered event revision cannot be negative")
        if self.last_delivered_revision > self.snapshot.revision:
            raise ValueError("Focus delivered event revision cannot exceed the current revision")


@dataclass(frozen=True, slots=True)
class FocusTurn:
    lease: FocusLease
    wake_reason: WakeReason
    read_after_row_id: int
    read_through_row_id: int
    events: tuple[FocusEventSnapshot, ...] = ()
    handoff_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    status: TurnStatus
    consumed_through_row_id: int | None = None
    delivered_event_revisions: Mapping[str, int] | None = None
    requeue: WakeReason = WakeReason.NONE


@dataclass(frozen=True, slots=True)
class FocusDispatch:
    managed: bool
    group_id: str | None = None
    active_chat_id: str | None = None
    woke_active: bool = False
    event: FocusEventSnapshot | None = None
    interrupt_active: bool = False


@dataclass(frozen=True, slots=True)
class SwitchChatRequest:
    """Terminal switch request produced from a server-issued event.

    target_chat_id is intentionally absent: callers and models cannot redirect
    a switch to an arbitrary stream.  The coordinator resolves it from event_id.
    """

    lease: FocusLease
    event_id: str
    expected_event_revision: int
    reasoning: str = ""


@dataclass(frozen=True, slots=True)
class SwitchResult:
    success: bool
    reason: str
    old_lease: FocusLease
    new_lease: FocusLease | None = None
    target_chat_id: str | None = None
    handoff_id: str | None = None


@dataclass(frozen=True, slots=True)
class UntrustedExcerpt:
    speaker_label: str
    text: str
    source_message_id: str


@dataclass(frozen=True, slots=True)
class HandoffPayload:
    task_summary: str = ""
    source_display_name: str = ""
    target_display_name: str = ""
    known_facts: tuple[str, ...] = ()
    pending_items: tuple[str, ...] = ()
    recent_results: tuple[str, ...] = ()
    excerpts: tuple[UntrustedExcerpt, ...] = ()

    def is_identity_only(self) -> bool:
        """Whether only the two transition display strings are populated."""

        return bool(self.source_display_name and self.target_display_name) and not (
            self.task_summary
            or self.known_facts
            or self.pending_items
            or self.recent_results
            or self.excerpts
        )


@dataclass(frozen=True, slots=True)
class FocusHandoff:
    handoff_id: str
    parent_id: str | None
    group_id: str
    source_chat_id: str
    target_chat_id: str
    source_epoch: int
    target_epoch: int
    payload: HandoffPayload
    policy_version: str
    created_at: float
    expires_at: float
    max_successful_cycles: int = 3
    revision: int = 1
    status: HandoffStatus = HandoffStatus.ACTIVE
    kind: HandoffKind = HandoffKind.CONTENT_V1

    def __post_init__(self) -> None:
        if self.target_epoch != self.source_epoch + 1:
            raise ValueError("Focus handoff target_epoch must immediately follow source_epoch")
        if self.expires_at <= self.created_at:
            raise ValueError("Focus handoff expiry must be after creation")
        if self.max_successful_cycles <= 0:
            raise ValueError("Focus handoff max_successful_cycles must be positive")


class FocusCoordinatorError(RuntimeError):
    """Base exception for Focus coordination failures."""


class UnknownFocusGroupError(FocusCoordinatorError):
    pass


class StaleFocusLeaseError(FocusCoordinatorError):
    pass


class FocusTransitionError(FocusCoordinatorError):
    pass


class FocusStoppedError(FocusCoordinatorError):
    pass
