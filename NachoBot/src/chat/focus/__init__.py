"""Public integration surface for Focus short-term cross-chat orchestration."""

from .coordinator import (
    EffectPermit,
    FocusCoordinator,
    FocusStateStore,
    bind_lease,
    current_context_lease,
    focus_coordinator,
)
from .handoff_builder import HandoffBuilder, HandoffLimits
from .handoff_store import HandoffStore, InMemoryHandoffStore
from .models import (
    ChatKind,
    EffectKind,
    FocusDispatch,
    FocusEventSnapshot,
    FocusGroupDefinition,
    FocusHandoff,
    FocusLease,
    FocusMember,
    FocusTurn,
    HandoffPayload,
    RestoredFocusEvent,
    StoredMessageRef,
    SwitchChatRequest,
    SwitchResult,
    TurnOutcome,
    TurnStatus,
    WakeReason,
)
from .scope_policy import ChatScopePolicy, ScopeDecision

__all__ = [
    "ChatKind",
    "ChatScopePolicy",
    "EffectKind",
    "EffectPermit",
    "FocusCoordinator",
    "FocusDispatch",
    "FocusEventSnapshot",
    "FocusGroupDefinition",
    "FocusHandoff",
    "FocusLease",
    "FocusMember",
    "FocusStateStore",
    "FocusTurn",
    "HandoffBuilder",
    "HandoffLimits",
    "HandoffPayload",
    "HandoffStore",
    "InMemoryHandoffStore",
    "ScopeDecision",
    "RestoredFocusEvent",
    "StoredMessageRef",
    "SwitchChatRequest",
    "SwitchResult",
    "TurnOutcome",
    "TurnStatus",
    "WakeReason",
    "bind_lease",
    "current_context_lease",
    "focus_coordinator",
]
