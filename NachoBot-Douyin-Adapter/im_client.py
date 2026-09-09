from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

from config import IMConfig


class DouyinIMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplyContext:
    user_open_id: str
    operator_open_id: str
    conversation_id: str
    message_id: str
    received_at: float


class DouyinIMClient:
    def __init__(self, config: IMConfig):
        self.config = config

    async def send_text(self, context: ReplyContext, text: str) -> dict[str, Any]:
        if not self.config.access_token:
            raise DouyinIMError("im.access_token is not configured")
        if not context.operator_open_id:
            raise DouyinIMError("im.operator_open_id is not configured")

        body = {
            "content": {"msg_type": 1, "text": {"text": text}},
            "to_user_id": context.user_open_id,
            "msg_id": context.message_id,
            "conversation_id": context.conversation_id,
            "scene": "im_reply_msg",
            "channel": 2,
        }
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        headers = {
            "Content-Type": "application/json",
            "access-token": self.config.access_token,
        }
        params = {"open_id": context.operator_open_id}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self.config.send_url, params=params, headers=headers, json=body
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except Exception as exc:
                    response_text = await response.text()
                    raise DouyinIMError(
                        f"Douyin IM returned HTTP {response.status}: {response_text[:300]}"
                    ) from exc

        if response.status >= 400:
            raise DouyinIMError(f"Douyin IM returned HTTP {response.status}: {payload}")
        extra = payload.get("extra") if isinstance(payload, dict) else None
        data = payload.get("data") if isinstance(payload, dict) else None
        error_code = 0
        description = ""
        if isinstance(extra, dict):
            error_code = int(extra.get("error_code") or 0)
            description = str(extra.get("description") or "")
        if not error_code and isinstance(data, dict):
            error_code = int(data.get("error_code") or 0)
            description = description or str(data.get("description") or "")
        if error_code:
            raise DouyinIMError(f"Douyin IM error {error_code}: {description or payload}")
        return payload
