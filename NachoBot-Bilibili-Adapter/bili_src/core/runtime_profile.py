"""Runtime capability declarations emitted by the Bilibili adapter.

This module intentionally has no dependency on NachoBot Core.  The dictionary
is a wire contract carried by ``BaseMessageInfo.additional_config``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


def build_live_additional_config(
    *,
    search_enabled: bool,
    person_profile_enabled: bool,
    tts_enabled: bool,
    tts_language: str = "",
    base: Optional[Mapping[str, Any]] = None,
    platform_event: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    language = str(tts_language or "").strip().lower()
    if language not in {"ja", "zh"}:
        language = ""

    additional = dict(base or {})
    additional["runtime_capabilities"] = {
        "schema_version": 1,
        "planner_bypass": True,
        "history_summarization": False,
        "notice_actions": False,
        "relation_inference": False,
        "expression_selection": False,
        "memory_retrieval": False,
        "mid_term_memory": True,
        "knowledge_retrieval": False,
        "reply_model_group": "realtime_replyer",
        "tool_mode": "mcp_only" if tts_enabled else "disabled",
        # Search orchestration is adapter-owned.  Core only transports the
        # adapter's JSON envelope when the live search protocol is enabled.
        "web_search_mode": "disabled",
        "reply_delivery": (
            "json_envelope"
            if search_enabled
            else ("aggregate_tagged_text" if tts_enabled else "chunked")
        ),
        "person_profile_mode": "low_latency" if person_profile_enabled else "disabled",
        "person_profile_timeout_seconds": 0.5,
        "typo_enabled": False,
        "tts_language": language if tts_enabled else "",
    }
    if platform_event:
        additional["platform_event"] = dict(platform_event)
    return additional
