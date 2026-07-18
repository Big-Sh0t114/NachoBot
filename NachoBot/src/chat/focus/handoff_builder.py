"""Build bounded, sanitized Focus handoffs from explicit planner output."""

from __future__ import annotations

import html
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Iterable

from .models import FocusHandoff, HandoffPayload, UntrustedExcerpt


@dataclass(frozen=True, slots=True)
class HandoffLimits:
    ttl_seconds: int = 600
    max_successful_cycles: int = 3
    prompt_token_budget: int = 512
    hard_prompt_token_cap: int = 768
    max_excerpts: int = 3
    raw_excerpts_enabled: bool = False

    def __post_init__(self) -> None:
        if self.ttl_seconds <= 0 or self.max_successful_cycles <= 0:
            raise ValueError("Handoff TTL and cycle limit must be positive")
        if not 0 < self.prompt_token_budget <= self.hard_prompt_token_cap:
            raise ValueError("Handoff prompt budget must be positive and below the hard cap")
        if self.max_excerpts < 0:
            raise ValueError("Handoff excerpt limit cannot be negative")


class HandoffBuilder:
    """Sanitizes planner-authored summaries; never accepts a raw prompt/runtime."""

    def __init__(self, limits: HandoffLimits | None = None) -> None:
        self.limits = limits or HandoffLimits()

    def build(
        self,
        *,
        group_id: str,
        source_chat_id: str,
        target_chat_id: str,
        source_epoch: int,
        policy_version: str,
        payload: HandoffPayload,
        parent: FocusHandoff | None = None,
        now: float | None = None,
    ) -> FocusHandoff:
        created_at = time.time() if now is None else now
        merged = self._merge_payload(parent.payload if parent else None, payload)
        bounded = self._bound_payload(merged)
        return FocusHandoff(
            handoff_id=uuid.uuid4().hex,
            parent_id=parent.handoff_id if parent else None,
            group_id=group_id,
            source_chat_id=source_chat_id,
            target_chat_id=target_chat_id,
            source_epoch=source_epoch,
            target_epoch=source_epoch + 1,
            payload=bounded,
            policy_version=policy_version,
            created_at=created_at,
            expires_at=created_at + self.limits.ttl_seconds,
            max_successful_cycles=self.limits.max_successful_cycles,
        )

    def _merge_payload(self, parent: HandoffPayload | None, delta: HandoffPayload) -> HandoffPayload:
        if parent is None:
            return delta
        return HandoffPayload(
            task_summary=delta.task_summary or parent.task_summary,
            source_display_name=delta.source_display_name or parent.source_display_name,
            target_display_name=delta.target_display_name or parent.target_display_name,
            known_facts=self._deduplicate((*parent.known_facts, *delta.known_facts)),
            pending_items=self._deduplicate((*parent.pending_items, *delta.pending_items)),
            recent_results=self._deduplicate((*delta.recent_results, *parent.recent_results)),
            excerpts=(*parent.excerpts, *delta.excerpts),
        )

    def _bound_payload(self, payload: HandoffPayload) -> HandoffPayload:
        # A conservative four characters/token approximation keeps the data
        # below the configured prompt budget even without a model tokenizer.
        remaining = self.limits.prompt_token_budget * 4

        def take(value: str, field_cap: int = 1000) -> str:
            nonlocal remaining
            if remaining <= 0:
                return ""
            clean = self._sanitize(value)
            length = min(len(clean), field_cap, remaining)
            result = clean[:length]
            remaining -= length
            return result

        source_display_name = take(payload.source_display_name, 160)
        target_display_name = take(payload.target_display_name, 160)
        task_summary = take(payload.task_summary, 1400)
        known_facts = tuple(item for value in payload.known_facts if (item := take(value, 360)))
        pending_items = tuple(item for value in payload.pending_items if (item := take(value, 360)))
        recent_results = tuple(item for value in payload.recent_results if (item := take(value, 360)))

        excerpts: list[UntrustedExcerpt] = []
        if self.limits.raw_excerpts_enabled:
            for excerpt in payload.excerpts[: self.limits.max_excerpts]:
                text = take(excerpt.text, 420)
                if not text:
                    break
                excerpts.append(
                    UntrustedExcerpt(
                        speaker_label=take(excerpt.speaker_label, 80),
                        text=text,
                        source_message_id=take(excerpt.source_message_id, 160),
                    )
                )

        return HandoffPayload(
            task_summary=task_summary,
            source_display_name=source_display_name,
            target_display_name=target_display_name,
            known_facts=known_facts,
            pending_items=pending_items,
            recent_results=recent_results,
            excerpts=tuple(excerpts),
        )

    @staticmethod
    def _sanitize(value: str) -> str:
        if not isinstance(value, str):
            value = str(value)
        filtered = "".join(
            character
            for character in value
            if character in {"\n", "\t"} or unicodedata.category(character) not in {"Cc", "Cf"}
        )
        normalized = " ".join(filtered.split())
        return html.escape(normalized, quote=True)

    @staticmethod
    def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            key = value.strip()
            if key and key not in seen:
                seen.add(key)
                result.append(value)
        return tuple(result)
