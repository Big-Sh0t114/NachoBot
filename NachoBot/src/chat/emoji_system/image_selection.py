"""Shared selection helpers for numbered image collages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from json_repair import repair_json


@dataclass(frozen=True)
class NumberedImageSelection:
    """Parsed result of asking a vision model to select from a numbered collage."""

    index: int | None = None
    descriptions: dict[int, str] = field(default_factory=dict)
    response: str = ""
    degraded: bool = False
    error: Exception | None = None


def parse_selected_index(response: str, candidate_count: int) -> int | None:
    """Return a zero-based index only for a strict, in-range JSON integer."""
    try:
        data = json.loads(repair_json(response))
        index = data.get("index") if isinstance(data, dict) else None
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if 1 <= index <= candidate_count:
            return index - 1
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def _parse_descriptions(data: Any, candidate_count: int) -> dict[int, str]:
    if not isinstance(data, dict):
        return {}

    raw_descriptions = data.get("descriptions")
    indexed: dict[int, str] = {}
    if isinstance(raw_descriptions, dict):
        entries = raw_descriptions.items()
    elif isinstance(raw_descriptions, list):
        entries = enumerate(raw_descriptions, start=1)
    else:
        return indexed

    for raw_index, raw_description in entries:
        if isinstance(raw_index, bool) or not isinstance(raw_description, str):
            continue
        try:
            numbered_index = int(raw_index)
        except (TypeError, ValueError):
            continue
        description = raw_description.strip()
        if 1 <= numbered_index <= candidate_count and description:
            indexed[numbered_index - 1] = description
    return indexed


def parse_numbered_selection(response: str, candidate_count: int) -> NumberedImageSelection:
    """Parse the index and optional per-candidate descriptions from one response."""
    try:
        data = json.loads(repair_json(response))
    except (json.JSONDecodeError, TypeError, ValueError):
        return NumberedImageSelection(response=response)

    return NumberedImageSelection(
        index=parse_selected_index(response, candidate_count),
        descriptions=_parse_descriptions(data, candidate_count),
        response=response,
    )


async def request_numbered_image_selection(
    image_base64: str,
    *,
    prompt: str,
    candidate_count: int,
    media_format: str,
    image_kind: Literal["emoji", "image"] = "emoji",
    metadata: dict[str, Any] | None = None,
) -> NumberedImageSelection:
    """Use the shared multimodal router and parse its numbered selection."""
    try:
        from src.multimodal import get_multimodal_router

        router = get_multimodal_router()
        describe = router.describe_emoji if image_kind == "emoji" else router.describe_image
        perceived = await describe(
            image_base64,
            prompt=prompt,
            media_format=media_format,
            metadata=metadata or {"temperature": 0, "max_tokens": 50},
        )
        response = getattr(perceived, "text", "") or ""
        if getattr(perceived, "degraded", False):
            return NumberedImageSelection(response=response, degraded=True)
        return parse_numbered_selection(response, candidate_count)
    except Exception as error:
        return NumberedImageSelection(error=error)
