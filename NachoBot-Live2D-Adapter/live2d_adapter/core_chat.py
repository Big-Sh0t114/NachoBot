"""Direct Core wire protocol for the desktop companion."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from typing import Any
from uuid import uuid4

from websockets.sync.client import connect

from .chat import ChatReply, DesktopPetBackendError
from .config import DesktopChatConfig
from .voice import DesktopVoice


def incoming_message(text: str, message_id: str, model_group: str = "") -> dict[str, Any]:
    platform = "local.live2d"
    return {
        "message_info": {
            "platform": platform,
            "message_id": message_id,
            "time": time.time(),
            "user_info": {"platform": platform, "user_id": "host", "user_nickname": "主人"},
            "group_info": {"platform": platform, "group_id": "desktop-pet", "group_name": "桌宠"},
            "format_info": {"content_format": ["text"], "accept_format": ["text", "reply"]},
            "additional_config": {
                "source": "live2d-desktop",
                "runtime_capabilities": {
                    "schema_version": 1,
                    "planner_bypass": True,
                    "reply_delivery": "json_envelope",
                    "history_summarization": False,
                    "notice_actions": False,
                    "relation_inference": False,
                    "expression_selection": False,
                    "memory_retrieval": False,
                    "mid_term_memory": False,
                    "knowledge_retrieval": False,
                    "reply_controls": True,
                    "tool_mode": "disabled",
                    "web_search_mode": "disabled",
                    "person_profile_mode": "disabled",
                    "typo_enabled": False,
                    **({"reply_model_group": model_group} if model_group else {}),
                    "control_emotions": ["normal", "joy", "shy", "sorrow", "angry", "surprise"],
                    "control_actions": ["none", "nod", "shake_head", "happy", "wink"],
                },
            },
        },
        "message_segment": {"type": "text", "data": text},
        "raw_message": text,
    }


def extract_reply(message: dict[str, Any], request_id: str) -> ChatReply | None:
    """Ignore expired, unattributed, and other users' replies instead of guessing."""
    if message.get("is_custom_message"):
        return None
    info = message.get("message_info") or {}
    if not isinstance(info, dict):
        return None
    additional = info.get("additional_config") or {}
    user = info.get("user_info") or {}
    if (
        not isinstance(additional, dict)
        or not isinstance(user, dict)
        or additional.get("reply_to_message_id") != request_id
        or str(user.get("user_id")) != "host"
    ):
        return None
    texts: list[str] = []

    def visit(value: Any) -> None:
        if not isinstance(value, dict):
            return
        kind, data = value.get("type"), value.get("data")
        if kind == "seglist" and isinstance(data, list):
            for child in data:
                visit(child)
        elif kind == "text" and isinstance(data, str):
            texts.append(data)
        elif kind == "tts_text" and isinstance(data, dict):
            texts.append(str(data.get("display_text") or data.get("text") or ""))

    visit(message.get("message_segment"))
    text = "\n".join(texts).strip()
    if not text:
        return None
    try:
        envelope = json.loads(text.strip("` \n").removeprefix("json").strip())
    except ValueError:
        envelope = None
    if isinstance(envelope, dict):
        reply = envelope.get("reply")
        if not isinstance(reply, str) or not reply.strip():
            return None
        emotion = envelope.get("emotion")
        action = envelope.get("action")
        return ChatReply(
            reply.strip()[:4000],
            emotion=emotion if isinstance(emotion, str) else None,
            action=action.upper() if isinstance(action, str) and action != "none" else None,
        )
    return ChatReply(text[:4000])


class CoreChatClient:
    def __init__(self, config: DesktopChatConfig, emit, logger, on_error) -> None:
        self.config = config
        self.voice = DesktopVoice(config, emit, logger, on_error)

    def ask(
        self, text: str, *, include_audio: bool = True, tts_language: str = "auto"
    ) -> ChatReply:
        token = self.voice.token()
        request_id = uuid4().hex
        headers = {"platform": "local.live2d"}
        auth = os.environ.get("NACHOBOT_CORE_TOKEN", "")
        if auth:
            headers["Authorization"] = auth
        try:
            with connect(
                self.config.core_url,
                additional_headers=headers,
                open_timeout=self.config.request_timeout_seconds,
                max_size=1048576,
            ) as socket:
                socket.send(
                    json.dumps(
                        incoming_message(text, request_id, self.config.core_reply_model_group),
                        ensure_ascii=False,
                    )
                )
                deadline = time.monotonic() + self.config.reply_timeout_seconds
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        message = json.loads(socket.recv(timeout=remaining))
                    except (ValueError, UnicodeError):
                        continue
                    reply = (
                        extract_reply(message, request_id) if isinstance(message, dict) else None
                    )
                    if reply is not None:
                        break
        except TimeoutError as exc:
            raise DesktopPetBackendError("Core 回答超时，请稍后重试。") from exc
        except Exception as exc:
            raise DesktopPetBackendError(f"无法连接 Core 聊天通道：{exc}") from exc
        pending = include_audio and self.voice.speak(reply.text, tts_language, token)
        return replace(reply, audio_pending=bool(pending))

    def announce(
        self, text: str, *, include_audio: bool = True, tts_language: str = "auto"
    ) -> ChatReply:
        pending = include_audio and self.voice.speak(text, tts_language, self.voice.token())
        return ChatReply(text, audio_pending=bool(pending))

    def set_voice_enabled(self, enabled: bool) -> None:
        self.voice.set_enabled(enabled)

    def close(self) -> None:
        self.voice.close()
