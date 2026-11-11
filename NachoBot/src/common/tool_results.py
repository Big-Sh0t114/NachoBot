"""Explicit outcome helpers shared by plugin tools and prompt renderers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_ERROR_TYPES = frozenset({"tool_error", "mcp_tool_error"})


def is_tool_error_result(result: Any) -> bool:
    """Return whether a mapping explicitly carries a reserved tool error type."""
    if not isinstance(result, Mapping):
        return False
    result_type = result.get("type")
    return type(result_type) is str and result_type in _ERROR_TYPES


def normalize_plugin_tool_result(
    result: Any,
    *,
    tool_call_id: Any,
    tool_name: str,
) -> dict[str, Any] | None:
    """Wrap a plugin result for the model while preserving explicit error status."""
    if not result:
        return None
    if not isinstance(result, Mapping):
        raise TypeError("plugin tool result must be a mapping")

    is_error = is_tool_error_result(result)
    if "content" in result:
        content = str(result["content"])
    elif "error" in result:
        content = str(result["error"])
    else:
        content = str(result)

    return {
        "tool_call_id": tool_call_id,
        "role": "tool",
        "name": tool_name,
        "type": "tool_error" if is_error else "function",
        "content": content,
    }
