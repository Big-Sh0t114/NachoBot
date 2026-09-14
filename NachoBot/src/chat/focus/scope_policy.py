"""Authorization policy for Focus events, switches, and context transfer."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    ChatKind,
    FocusGroupDefinition,
    FocusHandoff,
    FocusMember,
    HandoffKind,
    trusted_transition_labels,
)


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    allowed: bool
    reason: str


class ChatScopePolicy:
    """Default Focus scope policy with private-source identity switches.

    Membership is an allow-list, not just routing metadata.  The policy permits an
    enrolled group chat to switch to either another group chat or an enrolled
    private chat when configured. A private chat cannot export content, while a
    server-built transition identity handoff may accompany a switch to another
    enrolled member. Private->group and private->private content transfers
    remain denied.
    """

    version = "focus-scope-v3-private-transition-identity"

    def __init__(self, *, allow_group_to_private: bool = True) -> None:
        self._allow_group_to_private = bool(allow_group_to_private)

    def decide(
        self,
        definition: FocusGroupDefinition,
        source_chat_id: str,
        target_chat_id: str,
    ) -> ScopeDecision:
        members = {member.chat_id: member for member in definition.members}
        source = members.get(source_chat_id)
        target = members.get(target_chat_id)

        if source is None or target is None:
            return ScopeDecision(False, "source and target must belong to the same explicit Focus group")
        if source_chat_id == target_chat_id:
            return ScopeDecision(False, "source and target chats are identical")
        if not source.allow_export:
            return ScopeDecision(False, "source chat does not allow Focus context export")
        if not target.allow_import:
            return ScopeDecision(False, "target chat does not allow Focus context import")
        if source.kind is ChatKind.PRIVATE:
            return ScopeDecision(False, "private chats cannot export Focus context")
        if target.kind is ChatKind.PRIVATE and not self._allow_group_to_private:
            return ScopeDecision(False, "group-to-private Focus switching is disabled by policy")
        if source.kind is ChatKind.GROUP and target.kind in {ChatKind.GROUP, ChatKind.PRIVATE}:
            return ScopeDecision(True, "allowed by explicit Focus group policy")
        return ScopeDecision(False, "unsupported Focus scope transition")

    def decide_switch(
        self,
        definition: FocusGroupDefinition,
        source_chat_id: str,
        target_chat_id: str,
        *,
        has_handoff: bool,
        handoff_kind: HandoffKind | None = None,
    ) -> ScopeDecision:
        """Authorize a control-plane switch without weakening content policy."""

        if self.can_switch_without_handoff(definition, source_chat_id, target_chat_id):
            if has_handoff and handoff_kind is not HandoffKind.TRANSITION_IDENTITY_V1:
                return ScopeDecision(False, "private-source switch requires a transition identity handoff")
            if has_handoff:
                return ScopeDecision(True, "allowed as a private-source transition identity Focus switch")
            # This remains a routing/eligibility answer.  The commit boundary
            # requires the explicit identity handoff before changing state.
            return ScopeDecision(True, "allowed as a private-source Focus switch")
        return self.decide(definition, source_chat_id, target_chat_id)

    def can_switch_without_handoff(
        self,
        definition: FocusGroupDefinition,
        source_chat_id: str,
        target_chat_id: str,
    ) -> bool:
        """Whether a private source may switch without exporting any content."""

        source = self.member(definition, source_chat_id)
        target = self.member(definition, target_chat_id)
        return bool(
            source is not None
            and target is not None
            and source_chat_id != target_chat_id
            and source.kind is ChatKind.PRIVATE
            and target.kind in {ChatKind.GROUP, ChatKind.PRIVATE}
        )

    def can_preview_event(
        self,
        definition: FocusGroupDefinition,
        event_source_chat_id: str,
        viewer_chat_id: str,
    ) -> bool:
        """Authorize preview content in its real source-to-viewer direction."""

        if self.can_switch_without_handoff(definition, viewer_chat_id, event_source_chat_id):
            return False
        return self.decide(
            definition,
            event_source_chat_id,
            viewer_chat_id,
        ).allowed

    def can_emit_event(self, definition: FocusGroupDefinition, source_chat_id: str, target_chat_id: str) -> bool:
        """Whether enrolled activity may surface as a metadata-only event."""

        source = self.member(definition, source_chat_id)
        target = self.member(definition, target_chat_id)
        return bool(source is not None and target is not None and source_chat_id != target_chat_id)

    def can_switch(self, definition: FocusGroupDefinition, source_chat_id: str, target_chat_id: str) -> bool:
        return self.decide_switch(
            definition,
            source_chat_id,
            target_chat_id,
            has_handoff=False,
        ).allowed

    def can_transfer(self, definition: FocusGroupDefinition, source_chat_id: str, target_chat_id: str) -> bool:
        return self.decide(definition, source_chat_id, target_chat_id).allowed

    def can_inject(self, definition: FocusGroupDefinition, source_chat_id: str, target_chat_id: str) -> bool:
        """Reauthorize a handoff at injection time."""

        return self.decide(definition, source_chat_id, target_chat_id).allowed

    def authorize_handoff(self, definition: FocusGroupDefinition, handoff: FocusHandoff) -> bool:
        """Validate current policy and the explicit persisted handoff kind."""

        if handoff.policy_version != self.version:
            return False
        source = self.member(definition, handoff.source_chat_id)
        target = self.member(definition, handoff.target_chat_id)
        if source is None or target is None or source.chat_id == target.chat_id:
            return False

        if source.kind is ChatKind.PRIVATE:
            if target.kind not in {ChatKind.GROUP, ChatKind.PRIVATE}:
                return False
            if handoff.kind is not HandoffKind.TRANSITION_IDENTITY_V1:
                return False
            if handoff.parent_id is not None or not handoff.payload.is_identity_only():
                return False
            try:
                source_label, target_label = trusted_transition_labels(
                    definition,
                    handoff.source_chat_id,
                    handoff.target_chat_id,
                )
            except ValueError:
                return False
            return (
                handoff.payload.source_display_name == source_label
                and handoff.payload.target_display_name == target_label
            )

        if handoff.kind is not HandoffKind.CONTENT_V1:
            return False
        return self.decide(
            definition,
            handoff.source_chat_id,
            handoff.target_chat_id,
        ).allowed

    @staticmethod
    def member(definition: FocusGroupDefinition, chat_id: str) -> FocusMember | None:
        return next((member for member in definition.members if member.chat_id == chat_id), None)
