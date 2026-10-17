from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from loguru import logger

from config import AppConfig
from im_client import DouyinIMClient, ReplyContext
from outputs import ReplyOutput

ROOT_DIR = Path(__file__).resolve().parents[1]
NACHOBOT_DIR = ROOT_DIR / "NachoBot"
if str(NACHOBOT_DIR) not in sys.path:
    sys.path.insert(0, str(NACHOBOT_DIR))

from ncnk_message import (  # noqa: E402
    BaseMessageInfo,
    FormatInfo,
    GroupInfo,
    MessageBase,
    RouteConfig,
    Router,
    Seg,
    TargetConfig,
    TemplateInfo,
    UserInfo,
)


EVENT_LABELS = {
    "live_comment": "评论",
    "live_gift": "礼物",
    "live_like": "点赞",
    "live_fansclub": "粉丝团",
}


class DouyinAdapter:
    def __init__(self, config: AppConfig):
        self.config = config
        target = TargetConfig(
            url=f"ws://{config.nachobot.host}:{config.nachobot.port}/ws",
            token=None,
        )
        routes = {config.nachobot.platform: target}
        if config.im.enabled:
            routes[config.im.private_platform] = target
        self.router = Router(RouteConfig(route_config=routes))
        self.router.register_class_handler(self.handle_from_nachobot)
        self.queue: asyncio.Queue[tuple[str, str, dict[str, Any]]] = asyncio.Queue(
            maxsize=config.events.queue_size
        )
        self.output = ReplyOutput(config.output, config.tts, config.live2d)
        self.im_client = DouyinIMClient(config.im)
        self._private_contexts: dict[str, ReplyContext] = {}
        self._private_replied_message_ids: set[str] = set()
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._router_task: asyncio.Task | None = None
        self.received = 0
        self.forwarded = 0
        self.dropped = 0
        self.replies = 0

    async def start(self) -> None:
        self._router_task = asyncio.create_task(self._router_loop(), name="douyin-core-router")

    async def stop(self) -> None:
        if self._router_task:
            self._router_task.cancel()
            await asyncio.gather(self._router_task, return_exceptions=True)
        await self.router.stop()

    async def _router_loop(self) -> None:
        while True:
            try:
                await self.router.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("NachoBot core connection failed: {}; retrying in 3s", exc)
                await asyncio.sleep(3)

    def enqueue_payload(self, message_type: str, room_id: str, payload: Any) -> int:
        items = payload if isinstance(payload, list) else [payload]
        accepted = 0
        for item in items:
            if not isinstance(item, dict):
                self.dropped += 1
                continue
            message_id = str(item.get("msg_id") or "").strip()
            if message_id and self._is_duplicate(message_id):
                self.dropped += 1
                continue
            try:
                self.queue.put_nowait((message_type, room_id, item))
                accepted += 1
                self.received += 1
            except asyncio.QueueFull:
                self.dropped += 1
                logger.error("Douyin event queue is full; dropping {}", message_id or message_type)
        return accepted

    def enqueue_private_payload(self, payload: dict[str, Any]) -> int:
        content = payload.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                self.dropped += 1
                return 0
        if not isinstance(content, dict):
            self.dropped += 1
            return 0
        normalized = dict(payload)
        normalized["content"] = content
        message_id = str(
            payload.get("msg_id") or content.get("server_message_id") or ""
        ).strip()
        if message_id and self._is_duplicate(f"im:{message_id}"):
            self.dropped += 1
            return 0
        try:
            conversation_id = str(content.get("conversation_short_id") or "")
            self.queue.put_nowait(("im_receive_msg", conversation_id, normalized))
        except asyncio.QueueFull:
            self.dropped += 1
            logger.error("Douyin event queue is full; dropping private message {}", message_id)
            return 0
        self.received += 1
        return 1

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.config.douyin.dedup_ttl_seconds
        while self._seen:
            _, timestamp = next(iter(self._seen.items()))
            if timestamp >= cutoff:
                break
            self._seen.popitem(last=False)
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False

    async def consume_forever(self) -> None:
        while True:
            message_type, room_id, payload = await self.queue.get()
            try:
                message = self._build_message(message_type, room_id, payload)
                if message is None:
                    self.dropped += 1
                    continue
                await self.router.send_message(message)
                self.forwarded += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.dropped += 1
                logger.warning("Failed to forward Douyin event to core: {}", exc)
            finally:
                self.queue.task_done()

    def _build_message(
        self, message_type: str, room_id: str, payload: dict[str, Any]
    ) -> MessageBase | None:
        if message_type == "im_receive_msg":
            return self._build_private_message(payload)
        if not self._event_enabled(message_type, payload):
            return None
        user_id = str(payload.get("sec_openid") or payload.get("sec_open_id") or "anonymous")
        nickname = str(payload.get("nickname") or "抖音观众")
        message_id = str(payload.get("msg_id") or f"{message_type}-{time.time_ns()}")
        timestamp = float(payload.get("timestamp") or time.time())
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0

        text, event_data = self._event_text(message_type, payload, nickname)
        runtime_capabilities = {
            "schema_version": 1,
            "planner_bypass": True,
            "history_summarization": False,
            "notice_actions": False,
            "relation_inference": False,
            "expression_selection": False,
            "memory_retrieval": False,
            "mid_term_memory": True,
            "knowledge_retrieval": False,
            "reply_model_group": "realtime_replyer",
            "tool_mode": "mcp_only" if self.config.tts.enabled else "disabled",
            "web_search_mode": (
                "two_phase" if self.config.nachobot.network_search_enabled else "disabled"
            ),
            "reply_delivery": "aggregate_tagged_text",
            "person_profile_mode": (
                "low_latency" if self.config.nachobot.person_profile_enabled else "disabled"
            ),
            "person_profile_timeout_seconds": 0.5,
            "typo_enabled": False,
        }
        additional = {
            "room_id": room_id,
            "disable_tools": not self.config.nachobot.network_search_enabled,
            "runtime_capabilities": runtime_capabilities,
            "platform_event": event_data,
        }
        if message_type in {"live_gift", "live_fansclub"}:
            additional["is_mentioned"] = 1.0

        template = None
        if self.config.nachobot.reply_prompt.strip():
            reply_prompt = self.config.nachobot.reply_prompt
            replacements = {
                "{identity}": "",
                "{room_id}": room_id,
                "{event_type}": EVENT_LABELS.get(message_type, message_type),
            }
            for placeholder, value in replacements.items():
                reply_prompt = reply_prompt.replace(placeholder, value)
            template = TemplateInfo(
                template_items={
                    "replyer_prompt": reply_prompt,
                    "reply_prompt": reply_prompt,
                },
                template_name=f"douyin_live_{room_id}",
                template_default=False,
            )

        info = BaseMessageInfo(
            platform=self.config.nachobot.platform,
            message_id=message_id,
            time=timestamp,
            user_info=UserInfo(
                platform=self.config.nachobot.platform,
                user_id=user_id,
                user_nickname=nickname,
            ),
            group_info=GroupInfo(
                platform=self.config.nachobot.platform,
                group_id=room_id,
                group_name=f"抖音直播间 {room_id}",
            ),
            format_info=FormatInfo(content_format=["text"], accept_format=["text", "reply"]),
            template_info=template,
            additional_config=additional,
        )
        return MessageBase(message_info=info, message_segment=Seg(type="text", data=text))

    def _build_private_message(self, payload: dict[str, Any]) -> MessageBase | None:
        content = payload.get("content")
        if not isinstance(content, dict) or content.get("message_type") != "text":
            return None
        text = str(content.get("text") or "").strip()
        user_open_id = str(payload.get("from_user_id") or "").strip()
        operator_open_id = str(
            payload.get("to_user_id") or self.config.im.operator_open_id
        ).strip()
        conversation_id = str(content.get("conversation_short_id") or "").strip()
        server_message_id = str(content.get("server_message_id") or "").strip()
        if not all((text, user_open_id, conversation_id, server_message_id)):
            logger.warning("Dropped incomplete Douyin private message")
            return None

        nickname = "抖音用户"
        for user in content.get("user_infos") or []:
            if isinstance(user, dict) and str(user.get("open_id")) == user_open_id:
                nickname = str(user.get("nick_name") or nickname)
                break

        received_at = time.time()
        context = ReplyContext(
            user_open_id=user_open_id,
            operator_open_id=operator_open_id,
            conversation_id=conversation_id,
            message_id=server_message_id,
            received_at=received_at,
        )
        self._private_contexts[user_open_id] = context
        additional = {
            "is_mentioned": 1.0,
            "disable_tools": not self.config.nachobot.network_search_enabled,
            "runtime_capabilities": {
                "schema_version": 1,
                "reply_delivery": "aggregate_tagged_text",
                "reply_model_group": "realtime_replyer",
                "tool_mode": (
                    "mcp_only"
                    if self.config.nachobot.network_search_enabled
                    else "disabled"
                ),
                "web_search_mode": (
                    "two_phase"
                    if self.config.nachobot.network_search_enabled
                    else "disabled"
                ),
                "person_profile_mode": (
                    "low_latency"
                    if self.config.nachobot.person_profile_enabled
                    else "disabled"
                ),
            },
            "douyin_im_reply": {
                "user_open_id": user_open_id,
                "operator_open_id": operator_open_id,
                "conversation_id": conversation_id,
                "message_id": server_message_id,
                "received_at": received_at,
            },
            "platform_event": {"type": "im_receive_msg", "source": "douyin_webhook"},
        }
        template = None
        if self.config.im.reply_prompt:
            template = TemplateInfo(
                template_items={
                    "replyer_prompt": self.config.im.reply_prompt,
                    "reply_prompt": self.config.im.reply_prompt,
                },
                template_name="douyin_private_reply",
                template_default=False,
            )
        timestamp = float(content.get("create_time") or time.time())
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        info = BaseMessageInfo(
            platform=self.config.im.private_platform,
            message_id=server_message_id,
            time=timestamp,
            user_info=UserInfo(
                platform=self.config.im.private_platform,
                user_id=user_open_id,
                user_nickname=nickname,
            ),
            group_info=None,
            format_info=FormatInfo(content_format=["text"], accept_format=["text", "reply"]),
            template_info=template,
            additional_config=additional,
        )
        return MessageBase(message_info=info, message_segment=Seg(type="text", data=text))

    def _event_enabled(self, message_type: str, payload: dict[str, Any]) -> bool:
        if message_type == "live_comment":
            return self.config.events.comments and bool(str(payload.get("content") or "").strip())
        if message_type == "live_gift":
            return self.config.events.gifts
        if message_type == "live_like":
            return self.config.events.likes and int(payload.get("like_num") or 0) >= self.config.events.minimum_like_count
        if message_type == "live_fansclub":
            return self.config.events.fansclub
        return False

    @staticmethod
    def _event_text(
        message_type: str, payload: dict[str, Any], nickname: str
    ) -> tuple[str, dict[str, Any]]:
        event_data = {"type": message_type, "source": "douyin_official_live_data"}
        if message_type == "live_comment":
            content = str(payload.get("content") or "").strip()
            event_data["content"] = content
            return content, event_data
        if message_type == "live_gift":
            count = int(payload.get("gift_num") or 1)
            value = int(payload.get("gift_value") or 0)
            gift_id = str(payload.get("sec_gift_id") or "未知礼物")
            event_data.update({"gift_id": gift_id, "gift_num": count, "gift_value_fen": value})
            return f"{nickname}送出了礼物 {gift_id} × {count}，请自然地感谢对方。", event_data
        if message_type == "live_like":
            count = int(payload.get("like_num") or 0)
            event_data["like_num"] = count
            return f"{nickname}为直播间点赞了 {count} 次。", event_data
        level = int(payload.get("fansclub_level") or 1)
        reason = int(payload.get("fansclub_reason_type") or 0)
        event_data.update({"fansclub_level": level, "fansclub_reason_type": reason})
        action = "加入了粉丝团" if reason == 2 else f"粉丝团升级到 {level} 级"
        return f"{nickname}{action}，请自然地欢迎或感谢对方。", event_data

    async def handle_from_nachobot(self, raw_message: dict[str, Any]) -> None:
        try:
            message = MessageBase.from_dict(raw_message)
            text = self._plain_text(message.message_segment).strip()
            if not text:
                return
            text, emotion, action = self._parse_reply_metadata(text)
            self.replies += 1
            if message.message_info.platform == self.config.im.private_platform:
                context = self._resolve_private_context(message)
                if context is None:
                    logger.warning("Missing Douyin private reply context; reply was not sent")
                    return
                age = time.time() - context.received_at
                if age > self.config.im.reply_window_hours * 3600:
                    logger.warning("Douyin private reply context expired; reply was not sent")
                    return
                if context.message_id in self._private_replied_message_ids:
                    logger.warning("Duplicate core reply ignored for {}", context.message_id)
                    return
                reply = text[: self.config.im.max_reply_chars]
                await self.im_client.send_text(context, reply)
                self._private_replied_message_ids.add(context.message_id)
                logger.info("Sent Douyin private reply to {}", context.user_open_id)
                return
            await self.output.deliver(text, emotion=emotion, action=action)
        except Exception as exc:
            logger.exception("Failed to handle NachoBot reply: {}", exc)

    def _resolve_private_context(self, message: MessageBase) -> ReplyContext | None:
        additional = message.message_info.additional_config or {}
        raw = additional.get("douyin_im_reply")
        if isinstance(raw, dict):
            try:
                return ReplyContext(
                    user_open_id=str(raw["user_open_id"]),
                    operator_open_id=str(raw["operator_open_id"]),
                    conversation_id=str(raw["conversation_id"]),
                    message_id=str(raw["message_id"]),
                    received_at=float(raw["received_at"]),
                )
            except (KeyError, TypeError, ValueError):
                pass
        user = message.message_info.user_info
        if user and user.user_id:
            return self._private_contexts.get(str(user.user_id))
        return None

    @staticmethod
    def _parse_reply_metadata(text: str) -> tuple[str, str | None, str | None]:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return text, None, None
        try:
            data = json.loads(text[start : end + 1], strict=False)
        except (json.JSONDecodeError, TypeError):
            return text, None, None
        if not isinstance(data, dict) or not data.get("reply"):
            return text, None, None
        emotion = str(data["emotion"]) if data.get("emotion") is not None else None
        action = str(data["action"]) if data.get("action") is not None else None
        return str(data["reply"]), emotion, action

    @classmethod
    def _plain_text(cls, segment: Seg) -> str:
        if segment.type == "seglist" and isinstance(segment.data, list):
            return "".join(cls._plain_text(child) for child in segment.data)
        if segment.type in {"text", "reply", "tts_text"}:
            if isinstance(segment.data, dict):
                return str(segment.data.get("text") or segment.data.get("content") or "")
            return str(segment.data or "")
        return ""

    def status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "platform": self.config.nachobot.platform,
            "room_id": self.config.douyin.room_id,
            "queue_size": self.queue.qsize(),
            "received": self.received,
            "forwarded": self.forwarded,
            "dropped": self.dropped,
            "replies": self.replies,
            "im_enabled": self.config.im.enabled,
            "private_contexts": len(self._private_contexts),
        }
