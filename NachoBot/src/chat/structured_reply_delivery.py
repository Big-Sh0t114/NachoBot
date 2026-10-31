"""Versioned text/control segments; raw model JSON never enters dialogue history."""

from __future__ import annotations

import json
from typing import Any

from src.chat.runtime_capabilities import runtime_capabilities_from_message
from src.common.data_models.message_data_model import ReplyContentType


def normalize_reply(raw: str, request_id: str, capabilities: Any) -> dict[str, Any]:
    candidate = raw.strip()
    try:
        value = json.loads(candidate)
    except (ValueError, TypeError):
        value = {"text": candidate} if candidate and candidate[:1] not in '{[`' else None
    text = value.get("text") if isinstance(value, dict) else None
    if not isinstance(text, str) or not text.strip():
        return {"schema_version": 1, "request_id": request_id, "status": "error",
                "error": {"code": "invalid_reply", "message": "回复格式解析失败，请重试。"}}
    controls = value.get("controls")
    controls = controls if isinstance(controls, dict) else {}
    emotion, action = controls.get("emotion"), controls.get("action")
    emotion = emotion.lower() if isinstance(emotion, str) else ""
    action = action.upper() if isinstance(action, str) else ""
    return {"schema_version": 1, "request_id": request_id, "status": "ok", "text": text.strip(),
            "controls": {"emotion": emotion if emotion in capabilities.control_emotions else "",
                         "action": action if action in capabilities.control_actions else ""}}


async def deliver_structured_reply(reply_set: Any, message: Any, stream_id: str,
                                   need_reply: bool, selected_expressions: Any, receipts: list):
    from src.plugin_system.apis import send_api
    from src.chat.message_receive.uni_message_sender import _should_suppress_text_reply

    raw = "".join(str(item.content) for item in reply_set.reply_data if item.content_type == ReplyContentType.TEXT)
    payload = normalize_reply(raw, str(message.message_id), runtime_capabilities_from_message(message))
    text = payload.get("text", "")
    if text and _should_suppress_text_reply(text):
        text = payload["text"] = "Filtered"
        payload["controls"] = {}
    receipt = await send_api.structured_reply_to_stream_receipt(
        content=payload, stream_id=stream_id, reply_message=message, set_reply=need_reply,
        selected_expressions=selected_expressions, display_message=text,
    )
    receipts.append(receipt)
    return (text if receipt.delivered else ""), receipts
