"""Bounded extraction of one strict JSON object from model output."""

from __future__ import annotations

import json
import math
from typing import Any


MAX_MODEL_JSON_CHARS = 64 * 1024
MAX_MODEL_JSON_DEPTH = 64


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _find_single_container(content: str) -> tuple[int, int, str] | None:
    """Find one balanced maximal JSON container without interpreting its syntax."""

    stack: list[str] = []
    candidate_start = -1
    candidate_end = -1
    candidate_root = ""
    candidate_count = 0
    in_string = False
    escaped = False

    for index, char in enumerate(content):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char in "{[":
            if not stack:
                candidate_count += 1
                if candidate_count > 1:
                    return None
                candidate_start = index
                candidate_root = char
            stack.append("}" if char == "{" else "]")
            if len(stack) > MAX_MODEL_JSON_DEPTH:
                return None
            continue

        if char in "}]":
            if not stack or stack[-1] != char:
                return None
            stack.pop()
            if not stack and candidate_count == 1:
                candidate_end = index + 1

    if in_string or stack or candidate_count != 1:
        return None
    return candidate_start, candidate_end, candidate_root


def load_model_json_object(content: str) -> dict[str, Any] | None:
    """Extract exactly one bounded, strict JSON object from model output.

    Prose and Markdown fences around one object are allowed. Every container
    delimiter in the original text is still checked, so a second container,
    malformed wrapper, or unquoted bracket annotation rejects the response.
    """

    if not isinstance(content, str) or len(content) > MAX_MODEL_JSON_CHARS:
        return None

    span = _find_single_container(content)
    if span is None:
        return None
    start, end, root = span
    try:
        value = json.loads(
            content[start:end],
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
            parse_float=_parse_finite_float,
        )
    except (ValueError, RecursionError, OverflowError):
        return None
    if root != "{" or not isinstance(value, dict):
        return None
    return value
