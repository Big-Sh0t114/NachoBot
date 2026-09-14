"""Typed, call-local result for replyer prompt construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from src.chat.sandbox.sandbox_handoff import SandboxEditCandidate


@dataclass(frozen=True, slots=True)
class ReplyPromptBuildResult:
    """Prompt plus metadata that must stay attached to this generation call."""

    prompt: str
    selected_expressions: Optional[List[int]] = None
    sandbox_candidate: Optional[SandboxEditCandidate] = None


__all__ = ["ReplyPromptBuildResult"]
