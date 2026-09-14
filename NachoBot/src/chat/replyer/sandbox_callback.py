"""Replyer-owned bridge for returning user clarification to sandbox agents."""

from __future__ import annotations

from typing import Any

from src.chat.sandbox.sandbox_callback import sandbox_callback_registry


async def consume_sandbox_callback_reply(message: Any, chat_stream: Any) -> bool:
    """Return a pending sandbox clarification answer through the replyer boundary."""

    user_info = getattr(getattr(message, "message_info", None), "user_info", None)
    actor_id = str(getattr(user_info, "user_id", "") or "")
    if not actor_id:
        return False

    return await sandbox_callback_registry.deliver_reply(
        stream_id=str(getattr(chat_stream, "stream_id", "") or ""),
        platform=str(getattr(getattr(message, "message_info", None), "platform", "") or ""),
        actor_id=actor_id,
        text=str(getattr(message, "processed_plain_text", "") or ""),
    )


__all__ = ["consume_sandbox_callback_reply"]
