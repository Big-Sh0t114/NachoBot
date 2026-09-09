"""
NachoBot WebUI chat backend bridge.

The WebUI acts as a persistent ncnk_message adapter with platform "local".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tomlkit
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from memory_manager import _get_core_base_url

logger = logging.getLogger("webui.chat")

LOCAL_PLATFORM = "local"
DEFAULT_USER_ID = "webui-user"
DEFAULT_USER_NAME = "WebUI"
DEFAULT_FIRST_REPLY_TIMEOUT_SECONDS = 90.0
MAX_SUBSCRIBER_QUEUE_SIZE = 100
MAX_PENDING_REPLY_COUNT = 100
PENDING_REPLY_TTL_SECONDS = 180.0

_NACHOBOT_ROOT = Path(__file__).resolve().parent.parent / "NachoBot"
_BOT_CONFIG_PATH = _NACHOBOT_ROOT / "config" / "bot_config.toml"


class ChatBackendError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class LocalChatBackend:
    """Keep one `local` adapter connection and fan replies out to WebUI sessions."""

    def __init__(self) -> None:
        self._send_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._websocket: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._endpoint: str | None = None
        self._conversation_by_user: dict[str, str] = {}
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        # Keep a short replay window so multi-part replies are not lost while
        # the browser WebSocket is still connecting or briefly reconnecting.
        self._pending_replies: dict[str, list[tuple[float, dict[str, Any]]]] = {}

    async def status(self, core_running: bool) -> dict[str, Any]:
        try:
            endpoint, token = self._get_ncnk_ws_settings()
            return {
                "platform": LOCAL_PLATFORM,
                "endpoint": endpoint,
                "auth": bool(token),
                "core_running": core_running,
                "available": core_running,
                "connected": self._is_connected(),
            }
        except ChatBackendError as exc:
            return {
                "platform": LOCAL_PLATFORM,
                "core_running": core_running,
                "available": False,
                "error": str(exc),
            }

    def subscribe(self, conversation_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Subscribe a browser WebSocket to subsequent replies for one conversation."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=MAX_SUBSCRIBER_QUEUE_SIZE
        )
        self._subscribers.setdefault(conversation_id, set()).add(queue)
        self._replay_pending_replies(conversation_id, queue)
        return queue

    def unsubscribe(self, conversation_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(conversation_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(conversation_id, None)

    def acknowledge_live_delivery(self, conversation_id: str, message_id: str) -> None:
        """Remove one reply from replay storage after WebSocket delivery succeeds."""
        message_id = str(message_id or "")
        if not message_id:
            return
        pending = self._pending_replies.get(conversation_id)
        if not pending:
            return
        remaining = [
            item
            for item in pending
            if str(item[1].get("message_id") or "") != message_id
        ]
        if remaining:
            self._pending_replies[conversation_id] = remaining
        else:
            self._pending_replies.pop(conversation_id, None)

    def resolve_webui_user_id(self, conversation_id: str) -> str:
        """Return the stable Core-side user ID owned by a WebUI conversation."""
        return self._resolve_user_id(conversation_id or "default", DEFAULT_USER_ID)

    def forget_conversation(self, conversation_id: str) -> None:
        """Drop transient WebUI routing state after a conversation is deleted."""
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return

        backend_user_ids = [
            user_id
            for user_id, mapped_conversation_id in self._conversation_by_user.items()
            if mapped_conversation_id == conversation_id
        ]
        for user_id in backend_user_ids:
            self._conversation_by_user.pop(user_id, None)
        self._subscribers.pop(conversation_id, None)
        self._pending_replies.pop(conversation_id, None)

    async def close(self) -> None:
        """Close the persistent Core connection during WebUI shutdown."""
        async with self._connection_lock:
            websocket = self._websocket
            reader_task = self._reader_task
            self._websocket = None
            self._reader_task = None
            self._endpoint = None

        if reader_task and reader_task is not asyncio.current_task():
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        if websocket:
            try:
                await websocket.close()
            except Exception:
                pass

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        *,
        user_id: str = DEFAULT_USER_ID,
        user_name: str = DEFAULT_USER_NAME,
        request_message_id: str = "",
    ) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ChatBackendError("消息内容不能为空", status_code=400)

        conversation_id = conversation_id or "default"
        backend_user_id = self._resolve_user_id(conversation_id, user_id)
        message_id = str(request_message_id or "").strip() or f"webui-{uuid.uuid4().hex}"
        self._conversation_by_user[backend_user_id] = conversation_id

        try:
            endpoint, token = self._get_ncnk_ws_settings()
            payload = self._build_incoming_message(
                message_id=message_id,
                conversation_id=conversation_id,
                text=text,
                user_id=backend_user_id,
                user_name=user_name or DEFAULT_USER_NAME,
            )

            async with self._send_lock:
                websocket = await self._ensure_connection(endpoint, token)
                await websocket.send(json.dumps(payload, ensure_ascii=False))

            # POST 只负责可靠地把用户消息送入 Core。助手回复统一通过
            # /ws/chat/{conversation_id} 推送，避免 HTTP 等待期间锁死输入框，
            # 也避免首条回复同时经 HTTP 和 WebSocket 双通道竞争。
            return {
                "status": "accepted",
                "conversation_id": conversation_id,
                "platform": LOCAL_PLATFORM,
                "request_message_id": message_id,
            }
        except ChatBackendError:
            raise
        except OSError as exc:
            raise ChatBackendError(
                f"无法连接 NachoBot Core 聊天通道: {exc}", status_code=503
            ) from exc
        except ConnectionClosed as exc:
            raise ChatBackendError(
                f"NachoBot Core 聊天通道已断开: {exc}", status_code=502
            ) from exc
        except Exception as exc:
            logger.exception("Chat backend request failed")
            raise ChatBackendError(f"聊天后端请求失败: {exc}") from exc

    async def _ensure_connection(self, endpoint: str, token: str | None):
        async with self._connection_lock:
            if self._is_connected() and self._endpoint == endpoint:
                return self._websocket

            previous_websocket = self._websocket
            previous_reader = self._reader_task
            self._websocket = None
            self._reader_task = None
            self._endpoint = None

            if previous_reader:
                previous_reader.cancel()
                await asyncio.gather(previous_reader, return_exceptions=True)
            if previous_websocket:
                try:
                    await previous_websocket.close()
                except Exception:
                    pass

            headers = {"platform": LOCAL_PLATFORM}
            if token:
                headers["Authorization"] = token

            websocket = await self._connect(endpoint, headers)
            self._websocket = websocket
            self._endpoint = endpoint
            self._reader_task = asyncio.create_task(self._read_replies(websocket))
            logger.info("local chat adapter connected to %s", endpoint)
            return websocket

    def _is_connected(self) -> bool:
        return bool(
            self._websocket
            and self._reader_task
            and not self._reader_task.done()
        )

    async def _read_replies(self, websocket: Any) -> None:
        try:
            async for raw in websocket:
                message = self._loads_message(raw)
                event = self._make_reply_event(message)
                if event:
                    self._publish_reply(event)
        except ConnectionClosed as exc:
            logger.info("local chat adapter disconnected: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("local chat adapter reader failed")
        finally:
            if self._websocket is websocket:
                self._websocket = None
                self._reader_task = None
                self._endpoint = None

    def _make_reply_event(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if message.get("is_custom_message"):
            return None

        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return None
        user_info = message_info.get("user_info")
        if not isinstance(user_info, dict):
            return None

        user_id = str(user_info.get("user_id") or "")
        conversation_id = self._conversation_by_user.get(user_id)
        text = self._segment_to_text(message.get("message_segment")).strip()
        if not conversation_id or not text:
            return None

        additional_config = message_info.get("additional_config")
        reply_to_message_id = ""
        if isinstance(additional_config, dict):
            reply_to_message_id = str(additional_config.get("reply_to_message_id") or "")

        return {
            "type": "message",
            "conversation_id": conversation_id,
            "user_id": user_id,
            "message_id": str(message_info.get("message_id") or uuid.uuid4().hex),
            "reply_to_message_id": reply_to_message_id,
            "message": {
                "role": "assistant",
                "content": text,
            },
        }

    def _publish_reply(self, event: dict[str, Any]) -> None:
        if not str(event.get("reply_to_message_id") or ""):
            logger.warning(
                "Core reply %s has no reply_to_message_id; WebUI will append it without a turn anchor",
                event.get("message_id"),
            )

        conversation_id = event["conversation_id"]
        self._remember_pending_reply(conversation_id, event)
        for subscriber in tuple(self._subscribers.get(conversation_id, ())):
            self._put_nowait(subscriber, event)

    def _remember_pending_reply(self, conversation_id: str, event: dict[str, Any]) -> None:
        now = time.monotonic()
        pending = self._pending_replies.setdefault(conversation_id, [])
        pending.append((now, event))
        cutoff = now - PENDING_REPLY_TTL_SECONDS
        pending[:] = [item for item in pending[-MAX_PENDING_REPLY_COUNT:] if item[0] >= cutoff]

    def _replay_pending_replies(
        self,
        conversation_id: str,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        now = time.monotonic()
        cutoff = now - PENDING_REPLY_TTL_SECONDS
        pending = self._pending_replies.get(conversation_id, [])
        fresh = [item for item in pending if item[0] >= cutoff]
        if fresh:
            self._pending_replies[conversation_id] = fresh[-MAX_PENDING_REPLY_COUNT:]
            for _, event in self._pending_replies[conversation_id]:
                self._put_nowait(queue, event)
        else:
            self._pending_replies.pop(conversation_id, None)

    @staticmethod
    def _put_nowait(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def _get_ncnk_ws_settings(self) -> tuple[str, str | None]:
        ncnk_config = self._read_ncnk_config()
        token = os.getenv("NACHOBOT_CORE_TOKEN", "").strip() or self._first_auth_token(
            ncnk_config
        )

        if bool(ncnk_config.get("use_custom", False)):
            mode = str(ncnk_config.get("mode", "ws")).lower()
            if mode != "ws":
                raise ChatBackendError("WebUI Chat 暂只支持 ncnk_message 的 ws 模式")
            scheme = "wss" if bool(ncnk_config.get("use_wss", False)) else "ws"
            host = self._normalize_host(str(ncnk_config.get("host", "127.0.0.1")))
            port = int(ncnk_config.get("port", 8090))
            return f"{scheme}://{host}:{port}/ws", token

        core_base = _get_core_base_url()
        parsed = urlparse(core_base)
        host = self._normalize_host(parsed.hostname or "127.0.0.1")
        if parsed.port is None:
            raise ChatBackendError("无法确定 NachoBot Core 端口")
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{host}:{parsed.port}/ws", token

    def _read_ncnk_config(self) -> dict[str, Any]:
        if not _BOT_CONFIG_PATH.exists():
            return {}
        try:
            doc = tomlkit.parse(_BOT_CONFIG_PATH.read_text(encoding="utf-8"))
            section = doc.get("ncnk_message", {})
            if hasattr(section, "items"):
                return dict(section.items())
            if isinstance(section, dict):
                return dict(section)
            return {}
        except Exception as exc:
            logger.warning("Failed to read ncnk_message config: %s", exc)
            return {}

    @staticmethod
    def _first_auth_token(ncnk_config: dict[str, Any]) -> str | None:
        tokens = ncnk_config.get("auth_token") or []
        if isinstance(tokens, str):
            tokens = [tokens]
        for token in tokens:
            token_text = str(token).strip()
            if token_text:
                return token_text
        return None

    @staticmethod
    def _normalize_host(host: str) -> str:
        return "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host

    @staticmethod
    def _resolve_user_id(conversation_id: str, user_id: str) -> str:
        if user_id and user_id != DEFAULT_USER_ID:
            return user_id
        cleaned = "".join(
            char if char.isalnum() or char in "-_." else "_" for char in conversation_id
        ).strip("_")
        if not cleaned:
            return DEFAULT_USER_ID
        return f"webui_{cleaned[:96]}"

    @staticmethod
    def _build_incoming_message(
        *,
        message_id: str,
        conversation_id: str,
        text: str,
        user_id: str,
        user_name: str,
    ) -> dict[str, Any]:
        user_info = {
            "platform": LOCAL_PLATFORM,
            "user_id": user_id,
            "user_nickname": user_name,
            "user_cardname": user_name,
        }
        return {
            "message_info": {
                "platform": LOCAL_PLATFORM,
                "message_id": message_id,
                "time": round(time.time(), 3),
                "user_info": user_info,
                "format_info": {
                    "content_format": ["text"],
                    "accept_format": ["text"],
                },
                "additional_config": {
                    "source": "webui-chat",
                    "conversation_id": conversation_id,
                },
            },
            "message_segment": {
                "type": "text",
                "data": text,
            },
            "raw_message": text,
        }

    def _connect(self, endpoint: str, headers: dict[str, str]):
        try:
            return ws_connect(endpoint, additional_headers=headers, max_size=104_857_600)
        except TypeError:
            return ws_connect(endpoint, extra_headers=headers, max_size=104_857_600)

    @staticmethod
    def _loads_message(raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        return raw if isinstance(raw, dict) else {}

    def _segment_to_text(self, segment: Any) -> str:
        if not isinstance(segment, dict):
            return ""

        segment_type = segment.get("type")
        data = segment.get("data")
        if segment_type == "text":
            return str(data)
        if segment_type == "seglist" and isinstance(data, list):
            return "\n".join(
                text for text in (self._segment_to_text(item).strip() for item in data) if text
            )
        if segment_type == "reply":
            return ""
        if segment_type == "image":
            return "[图片]"
        if segment_type == "emoji":
            return "[表情]"
        if segment_type == "voice":
            return "[语音]"
        if segment_type == "video":
            return "[视频]"
        if segment_type == "file":
            return "[文件]"
        if data is None:
            return ""
        return str(data)


chat_backend = LocalChatBackend()
