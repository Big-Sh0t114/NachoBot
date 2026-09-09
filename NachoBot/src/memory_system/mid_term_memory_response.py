"""Pure parsing helpers for mid-term-memory LLM responses."""

import json
from typing import List, Tuple

from json_repair import repair_json


def _parse_summary_response(response: str) -> Tuple[str, List[str]]:
    """Parse and validate a mid-term-memory summary response.

    Strict JSON parsing is attempted first.  The json-repair fallback is used
    only when strict parsing raises ``json.JSONDecodeError``.  Both paths then
    share the same structural validation before any cue can be vectorized.

    Args:
        response: Raw response returned by the summary LLM.

    Returns:
        ``(summary, recall_cues)`` with only non-empty string cues retained.

    Raises:
        ValueError: If the parsed response or required fields have invalid
            types, or if the summary is empty.
        json.JSONDecodeError: If strict parsing and repair parsing both fail.
    """
    response_text = response.strip()
    if response_text.startswith("```json"):
        response_text = response_text[7:]
    if response_text.startswith("```"):
        response_text = response_text[3:]
    if response_text.endswith("```"):
        response_text = response_text[:-3]
    response_text = response_text.strip()

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        repaired_response = repair_json(response_text)
        result = (
            json.loads(repaired_response)
            if isinstance(repaired_response, str)
            else repaired_response
        )

    if not isinstance(result, dict):
        raise ValueError("中期记忆摘要 JSON 顶层必须是对象")

    summary_text = result.get("summary", "")
    if not isinstance(summary_text, str) or not summary_text.strip():
        raise ValueError("中期记忆摘要 summary 必须是非空字符串")

    recall_cues_text = result.get("recall_cues", [])
    if not isinstance(recall_cues_text, list):
        raise ValueError("中期记忆摘要 recall_cues 必须是列表")

    return summary_text, [
        cue for cue in recall_cues_text if isinstance(cue, str) and cue.strip()
    ]
