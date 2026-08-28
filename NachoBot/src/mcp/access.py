"""Build MCP authorization context from a live chat stream."""

from __future__ import annotations

from typing import Any

from src.mcp.types import MCPAccessContext


def access_context_from_stream(chat_stream: Any, user_id: str = "") -> MCPAccessContext:
    """Resolve platform identity once, before catalog discovery and execution."""
    resolved_user_id = str(user_id or "")
    if not resolved_user_id and getattr(chat_stream, "user_info", None):
        resolved_user_id = str(getattr(chat_stream.user_info, "user_id", "") or "")

    group_info = getattr(chat_stream, "group_info", None)
    group_id = str(getattr(group_info, "group_id", "") or "") if group_info else ""
    is_group = bool(group_id)
    chat_id = group_id or resolved_user_id or str(getattr(chat_stream, "stream_id", "") or "")

    try:
        from src.config.config import global_config

        is_admin = resolved_user_id in {str(value) for value in global_config.advanced.admins}
    except Exception:
        is_admin = False

    return MCPAccessContext(
        user_id=resolved_user_id,
        chat_id=chat_id,
        is_group=is_group,
        is_admin=is_admin,
    )
