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
from .config import DEFAULT_ACTION_MAPPINGS


def incoming_message(text: str, message_id: str, model_group: str = "", desktop_context: dict | None = None) -> dict[str, Any]:
    platform = "local.live2d"
    return {
        "message_info": {
            "platform": platform,
            "message_id": message_id,
            "time": time.time(),
            "user_info": {"platform": platform, "user_id": "host", "user_nickname": "用户", "user_cardname": "用户"},
            "format_info": {"content_format": ["text"], "accept_format": ["structured_reply", "reply"]},
            "additional_config": {
                "source": "live2d-desktop",
                "conversation_id": "desktop-companion",
                **({"desktop_context": desktop_context} if desktop_context else {}),
                "runtime_capabilities": {
                    "schema_version": 1,
                    "planner_bypass": True,
                    "prompt_profile": "desktop_companion",
                    "reply_delivery": "structured_reply",
                    "reply_controls": True,
                    **({"reply_model_group": model_group} if model_group else {}),
                    "control_emotions": ["normal", "joy", "shy", "sorrow", "angry", "surprise"],
                    "control_actions": list(DEFAULT_ACTION_MAPPINGS),
                },
            },
        },
        "message_segment": {"type": "text", "data": text},
        "raw_message": text,
    }


def incoming_idle_event(message_id: str, idle_seconds: float, model_group: str = "", desktop_context: dict | None = None) -> dict[str, Any]:
    packet = incoming_message("", message_id, model_group, desktop_context)
    info = packet["message_info"]
    info.pop("user_info")
    info["additional_config"].update({
        "system_event": {"version": 1, "type": "desktop.idle", "actor": None, "target": None,
                         "data": {"idle_seconds": max(0, min(86400, int(idle_seconds)))}},
        "system_event_route": {"platform": "local.live2d", "kind": "private",
                               "peer": {"user_id": "host", "nickname": "用户"}},
    })
    return packet


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
    segments = []
    def visit(segment: Any) -> None:
        if not isinstance(segment, dict):
            return
        if segment.get("type") == "seglist" and isinstance(segment.get("data"), list):
            for child in segment["data"]:
                visit(child)
        elif segment.get("type") == "structured_reply" and isinstance(segment.get("data"), dict):
            segments.append(segment["data"])
    visit(message.get("message_segment"))
    for data in segments:
        if data.get("request_id") != request_id:
            continue
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise DesktopPetBackendError("Core 桌宠消息协议版本不兼容，请同步更新 Core。")
        if data.get("status") == "error":
            raise DesktopPetBackendError("Core 回复格式解析失败，请重试。")
        text = data.get("text")
        if data.get("status") != "ok" or not isinstance(text, str) or not text.strip():
            continue
        controls = data.get("controls") or {}
        if not isinstance(controls, dict):
            controls = {}
        emotion, action = controls.get("emotion"), controls.get("action")
        return ChatReply(text.strip()[:4000],
                         emotion=emotion if isinstance(emotion, str) and emotion in {"normal","joy","shy","sorrow","angry","surprise"} else None,
                         action=action if isinstance(action, str) and action in DEFAULT_ACTION_MAPPINGS else None)
    return None


class CoreChatClient:
    def __init__(self, config: DesktopChatConfig, emit, logger, on_error, context_provider=None) -> None:
        self.config = config
        self.context_provider = context_provider
        self.voice = DesktopVoice(config, emit, logger, on_error)

    def ask(
        self, text: str, *, include_audio: bool = True, tts_language: str = "auto"
    ) -> ChatReply:
        request_id = uuid4().hex
        packet = incoming_message(text, request_id, self.config.core_reply_model_group,
                                  self.context_provider() if self.context_provider else None)
        return self._request(packet, include_audio=include_audio, tts_language=tts_language)

    def proactive(self, idle_seconds: float, *, include_audio: bool = True,
                  tts_language: str = "auto", is_cancelled=lambda: False) -> ChatReply | None:
        packet = incoming_idle_event(uuid4().hex, idle_seconds, self.config.core_reply_model_group,
                                     self.context_provider() if self.context_provider else None)
        return self._request(packet, include_audio=include_audio, tts_language=tts_language,
                             is_cancelled=is_cancelled)

    def _request(self, packet: dict, *, include_audio: bool, tts_language: str,
                 is_cancelled=None) -> ChatReply | None:
        token = self.voice.token()
        request_id = packet["message_info"]["message_id"]
        if is_cancelled and is_cancelled():
            return None
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
                        packet,
                        ensure_ascii=False,
                    )
                )
                deadline = time.monotonic() + self.config.reply_timeout_seconds
                while True:
                    if is_cancelled and is_cancelled():
                        return None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        message = json.loads(socket.recv(timeout=min(remaining, 0.25) if is_cancelled else remaining))
                    except TimeoutError:
                        if is_cancelled:
                            continue
                        raise
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
        if is_cancelled and is_cancelled():
            return None
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
