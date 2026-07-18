"""Authorization policy for Focus events, switches, and context transfer."""

from __future__ import annotations

from dataclasses import dataclass

from .models import ChatKind, FocusGroupDefinition, FocusMember


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    allowed: bool
    reason: str


class ChatScopePolicy:
    """Default v1 Focus scope policy.

    Membership is an allow-list, not just routing metadata.  v1 permits an
    enrolled group chat to switch to either another group chat or an enrolled
    private chat when configured. A private chat remains a content-export
    terminal, while a metadata-only, handoff-free control return to an enrolled
    group is permitted. Private->group and private->private content transfers
    remain denied.
    """

    version = "focus-scope-v1-group-export"

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
            return ScopeDecision(False, "private chats cannot export Focus context in policy v1")
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
    ) -> ScopeDecision:
        """Authorize a control-plane switch without weakening content policy."""

        if self.can_return_without_handoff(definition, source_chat_id, target_chat_id):
            if has_handoff:
                return ScopeDecision(False, "private-to-group safe return must not include a handoff")
            return ScopeDecision(True, "allowed as a metadata-only Focus safe return")
        return self.decide(definition, source_chat_id, target_chat_id)

    def can_return_without_handoff(
        self,
        definition: FocusGroupDefinition,
        source_chat_id: str,
        target_chat_id: str,
    ) -> bool:
        """Whether an enrolled private active chat may return to a group."""

        source = self.member(definition, source_chat_id)
        target = self.member(definition, target_chat_id)
        return bool(
            source is not None
            and target is not None
            and source_chat_id != target_chat_id
            and source.kind is ChatKind.PRIVATE
            and target.kind is ChatKind.GROUP
        )

    def can_preview_event(
        self,
        definition: FocusGroupDefinition,
        event_source_chat_id: str,
        viewer_chat_id: str,
    ) -> bool:
        """Authorize preview content in its real source-to-viewer direction."""

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
        return self.decide(definition, source_chat_id, target_chat_id).allowed

    def can_transfer(self, definition: FocusGroupDefinition, source_chat_id: str, target_chat_id: str) -> bool:
        return self.decide(definition, source_chat_id, target_chat_id).allowed

    def can_inject(self, definition: FocusGroupDefinition, source_chat_id: str, target_chat_id: str) -> bool:
        """Reauthorize a handoff at injection time."""

        return self.decide(definition, source_chat_id, target_chat_id).allowed

    @staticmethod
    def member(definition: FocusGroupDefinition, chat_id: str) -> FocusMember | None:
        return next((member for member in definition.members if member.chat_id == chat_id), None)
