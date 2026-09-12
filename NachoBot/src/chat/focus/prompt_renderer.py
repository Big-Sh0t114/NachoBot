"""Render authorized Focus handoffs as bounded natural-language prose."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import Iterable

from .models import FocusHandoff, HandoffKind, HandoffPayload


# These patterns are intentionally used only to compute the result flag.  The
# renderer must not append the generic guard's safety notice to model-visible
# Focus prose; current-message/history guardrails remain responsible for that
# separate context.
_PROMPT_INJECTION_RULES = (
    r"(忽略|忘记).{0,8}(之前|以上).{0,4}(指令|提示|设定|规则)",
    r"(移除|删除|替换|覆盖|重置).{0,6}(系统|规则|设定|人格|人设)",
    r"(切换|改变|修改).{0,6}(人格|人设|身份|角色)",
    r"(从现在开始|接下来).{0,6}(扮演|假装|充当)",
    r"(system prompt|system message|developer mode|dev mode|jailbreak)",
    r"(遵循|执行).{0,6}(以下规则|新的规则|新的指令)",
)

_FOCUS_BLOCK_RE = re.compile(r"<focus_handoff\b(?P<attrs>[^>]*)>.*?</focus_handoff>", re.DOTALL)
_ATTR_RE = re.compile(r'(?P<name>[a-z_]+)="(?P<value>[^"]*)"')
_UNTRUSTED_PAYLOAD_RE = re.compile(r"<untrusted_payload>\n?(?P<body>.*?)\n?</untrusted_payload>", re.DOTALL)
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


@dataclass(frozen=True, slots=True)
class RenderedFocusHandoff:
    block: str
    injection_detected: bool
    estimated_tokens: int
    digest: str


def render_focus_handoffs(
    handoffs: Iterable[FocusHandoff],
    *,
    max_tokens: int = 512,
) -> RenderedFocusHandoff:
    """Render one or more already-authorized handoffs as plain prose.

    Authorization is performed by the ReplyContext provider/coordinator.  This
    function only normalizes, bounds, detects suspicious text for telemetry,
    and computes a deterministic digest.  Detection never adds a warning to
    the returned model prompt.
    """

    if max_tokens < 128 or max_tokens > 768:
        raise ValueError("Focus handoff prompt token budget must be within 128..768")

    handoff_list = tuple(handoffs)
    if not handoff_list:
        return RenderedFocusHandoff(block="", injection_detected=False, estimated_tokens=0, digest="")

    char_budget = max_tokens * 4
    # A private-source identity handoff is deliberately rendered on its own.
    # Even if a caller accidentally supplies another handoff in the iterable,
    # no content can be joined to the identity sentence.
    identities = [item for item in handoff_list if item.kind is HandoffKind.TRANSITION_IDENTITY_V1]
    if identities:
        identity = identities[-1]
        source = _normalize(identity.payload.source_display_name, 160) or "上一私聊"
        target = _normalize(identity.payload.target_display_name, 160) or "当前私聊"
        block = f"你刚刚从{source}切换至{target}。"
        block = block[:char_budget]
        return _rendered(block)

    sections: list[str] = []
    remaining = char_budget
    for handoff in handoff_list:
        section = _render_payload(handoff.payload, remaining)
        if section:
            sections.append(section)
            remaining -= len(section)
        if remaining <= 0:
            break

    block = " ".join(sections).strip()[:char_budget]
    return _rendered(block)


def _rendered(block: str) -> RenderedFocusHandoff:
    normalized = block.strip()
    injection_detected = _detect_injection(normalized)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16] if normalized else ""
    estimated_tokens = max(1, (len(normalized) + 3) // 4) if normalized else 0
    return RenderedFocusHandoff(
        block=normalized,
        injection_detected=injection_detected,
        estimated_tokens=estimated_tokens,
        digest=digest,
    )


def redact_focus_handoff_block(
    prompt: str,
    exact_block: str,
    *,
    digest: str = "",
    estimated_tokens: int | None = None,
) -> str:
    """Redact one exact plain-text block from a prompt log.

    New renderer output has no wrapper that can be found safely with a tag
    parser.  Callers should pass the exact block captured alongside its digest;
    the legacy tag helper below remains only for old prompt strings.
    """

    if not prompt or not exact_block:
        return prompt
    digest = digest or hashlib.sha256(exact_block.encode("utf-8")).hexdigest()[:16]
    token_count = estimated_tokens if estimated_tokens is not None else max(1, (len(exact_block) + 3) // 4)
    replacement = (
        f"[FOCUS_HANDOFF_REDACTED_LOG_ONLY digest={digest} "
        f"estimated_tokens={token_count} llm_payload=full_authorized_handoff]"
    )
    return prompt.replace(exact_block, replacement, 1)


def redact_focus_handoff_blocks(prompt: str) -> str:
    """Redact legacy tagged Focus blocks only.

    Plain-text blocks cannot be identified from an arbitrary prompt without an
    exact block argument; use :func:`redact_focus_handoff_block` for those.
    """

    if not prompt or "<focus_handoff" not in prompt:
        return prompt

    def _replacement(match: re.Match[str]) -> str:
        attrs = {item.group("name"): item.group("value") for item in _ATTR_RE.finditer(match.group("attrs"))}
        payload_match = _UNTRUSTED_PAYLOAD_RE.search(match.group(0))
        guarded_payload = html.unescape(payload_match.group("body")) if payload_match else ""
        handoff_ids = attrs.get("handoff_ids", "omitted")
        digest = attrs.get("digest") or hashlib.sha256(guarded_payload.encode("utf-8")).hexdigest()[:16]
        token_count = attrs.get("estimated_tokens") or str(max(1, (len(match.group(0)) + 3) // 4))
        return (
            f"[FOCUS_HANDOFF_REDACTED_LOG_ONLY ids={handoff_ids} digest={digest} "
            f"estimated_tokens={token_count} llm_payload=full_authorized_handoff]"
        )

    return _FOCUS_BLOCK_RE.sub(_replacement, prompt)


def _render_payload(payload: HandoffPayload, budget: int) -> str:
    if budget <= 0:
        return ""

    source = _normalize(payload.source_display_name, 160) or "上一会话"
    target = _normalize(payload.target_display_name, 160) or "当前会话"
    clauses = [f"你刚刚从{source}切换至{target}。"]

    summary = _normalize(payload.task_summary, 800)
    if summary:
        clauses.append(f"需要继续处理{summary}。")

    facts = [_normalize(value, 320) for value in payload.known_facts[:8]]
    facts = [value for value in facts if value]
    if facts:
        clauses.append(f"已确认{_join_natural(facts)}。")

    pending = [_normalize(value, 320) for value in payload.pending_items[:8]]
    pending = [value for value in pending if value]
    if pending:
        clauses.append(f"接下来要关注{_join_natural(pending)}。")

    recent = [_normalize(value, 320) for value in payload.recent_results[:10]]
    recent = [value for value in recent if value]
    for excerpt in payload.excerpts[:3]:
        speaker = _normalize(excerpt.speaker_label, 48) or "对方"
        text = _normalize(excerpt.text, 240)
        if text:
            recent.append(f"{speaker}说“{text}”")
    if recent:
        clauses.append(f"最近进展是{_join_natural(recent)}。")

    return " ".join(clauses)[:budget].rstrip()


def _join_natural(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    return "；".join(values)


def _detect_injection(value: str) -> bool:
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in _PROMPT_INJECTION_RULES)


def _normalize(value: object, limit: int) -> str:
    text = html.unescape(str(value or ""))
    text = _UNSAFE_CONTROL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Keep model-visible Focus context plain text even when a configured label
    # or old payload contains angle-bracket markup.
    text = text.replace("<", "＜").replace(">", "＞")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


__all__ = [
    "RenderedFocusHandoff",
    "redact_focus_handoff_block",
    "redact_focus_handoff_blocks",
    "render_focus_handoffs",
]
