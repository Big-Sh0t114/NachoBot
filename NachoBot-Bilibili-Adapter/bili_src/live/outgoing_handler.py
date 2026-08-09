import asyncio
from loguru import logger
import time
from typing import Any, Dict, Optional

from ncnk_message import MessageBase
from bili_src.core.utils import (
    _extract_image_base64,
    _extract_plain_text,
    _find_reply_id,
    _strip_emoji,
    _split_bilibili_text,
    BILIBILI_DANMU_MAX_LENGTH,
    BILIBILI_DANMU_SEND_DELAY_SECONDS,
)
from bili_src.live.two_phase_search import BilibiliLiveSearchOrchestrator


class OutgoingHandler:
    def __init__(self, config: Any, logger, adapter_ref: Any):
        self.config = config
        self.logger = logger
        self.adapter = adapter_ref
        self.live_search = BilibiliLiveSearchOrchestrator(adapter_ref, logger)

    async def handle_from_nachobot(self, raw_message_base_dict: dict) -> None:
        message = MessageBase.from_dict(raw_message_base_dict)
        self.logger.info(
            "Incoming from NachoBot: platform=%s group_id=%s user_id=%s",
            message.message_info.platform,
            getattr(message.message_info.group_info, "group_id", None),
            getattr(message.message_info.user_info, "user_id", None),
        )
        seg = message.message_segment
        if seg.type == "command":
            await self._handle_command(message)
            return

        image_data = _extract_image_base64(seg)
        if image_data:
            private_target = self.adapter.private_handler.resolve_private_target(
                message
            )
            if private_target:
                await self.adapter.private_handler.send_private_image(
                    private_target, image_data
                )
                text = _extract_plain_text(seg).strip()
                if text:
                    await self.adapter.private_handler.send_private_message(
                        private_target, text
                    )
            else:
                self.logger.warning("Image message unsupported for non-private target")
            return

        text = _extract_plain_text(seg).strip()
        if not text:
            return

        original_text = text
        text, emotion, action = (
            self.adapter.live2d_manager.extract_json_emotion_from_text(original_text)
        )

        comment_target = self.adapter.comment_handler.resolve_comment_target(message)
        if comment_target:
            self.adapter.live2d_manager.execute_extracted_live2d_action(emotion, action)
            await self.adapter.comment_handler.send_comment_reply_from_context(
                comment_target, text
            )
            return

        room_id = self._resolve_room_id(message)
        if room_id is not None:
            reply_dmid = _find_reply_id(seg)
            reply_mid = ""
            if reply_dmid:
                reply_mid = self._lookup_reply_mid(room_id, reply_dmid)
            await self._handle_live_reply(
                {
                    "message": original_text,
                    "room_id": room_id,
                    "reply_mid": reply_mid or "",
                    "reply_dmid": reply_dmid or "",
                }
            )
            return

        private_target = self.adapter.private_handler.resolve_private_target(message)
        if private_target:
            self.adapter.live2d_manager.execute_extracted_live2d_action(emotion, action)
            await self.adapter.private_handler.send_private_message(
                private_target, text
            )
            return

        self.logger.warning("Missing room_id for outgoing danmu")

    def _resolve_room_id(self, message: MessageBase) -> Optional[int]:
        group_info = message.message_info.group_info
        if group_info and group_info.group_id:
            try:
                return int(group_info.group_id)
            except ValueError:
                return None
        additional = message.message_info.additional_config or {}
        room_id = additional.get("room_id")
        if room_id is None:
            return None
        try:
            return int(room_id)
        except ValueError:
            return None

    def _lookup_reply_mid(self, room_id: int, reply_dmid: str) -> str:
        cache = self.adapter._danmu_cache.get(room_id) or {}
        return str(cache.get(reply_dmid) or "")

    async def _handle_command(self, message: MessageBase) -> None:
        seg = message.message_segment
        command_data = seg.data if isinstance(seg.data, dict) else {}
        command_name = str(command_data.get("name") or "")
        args = command_data.get("args") or {}
        if command_name == "BILI_COMMENT_REPLY":
            await self.adapter._handle_comment_reply(args)
            return
        if command_name == "BILI_LIVE_REPLY":
            await self._handle_live_reply(args)
            return
        if command_name == "BILI_PRIVATE_SEND":
            await self.adapter.private_handler.handle_private_send(args, message)
            return
        self.logger.warning(f"Unknown command: {command_name}")

    async def _handle_comment_reply(self, args: Dict[str, Any]) -> None:
        raw_msg = str(args.get("message") or "")
        text, emotion, action = (
            self.adapter.live2d_manager.extract_json_emotion_from_text(raw_msg)
        )
        text = _strip_emoji(text).strip()

        self.adapter.live2d_manager.execute_extracted_live2d_action(emotion, action)

        if not text:
            self.logger.warning("Empty comment reply text")
            return
        text = self.adapter._filter_outgoing_text(text)
        try:
            comment_type = int(args.get("type"))
            oid = int(args.get("oid"))
        except (TypeError, ValueError):
            self.logger.warning("Invalid comment reply args")
            return
        root = args.get("root")
        parent = args.get("parent")
        root_id = int(root) if root not in (None, "", 0) else None
        parent_id = int(parent) if parent not in (None, "", 0) else None
        target = (comment_type, oid, root_id, parent_id)
        await self.adapter.comment_handler.send_comment_reply_from_context(target, text)

    async def _handle_live_reply(self, args: Dict[str, Any]) -> None:
        raw_message = str(args.get("message") or "")
        try:
            room_id = int(args.get("room_id"))
        except (TypeError, ValueError):
            self.logger.warning("Invalid room_id for live reply")
            return
        reply_mid = str(args.get("reply_mid") or "")
        reply_dmid = str(args.get("reply_dmid") or "")

        handled = await self.live_search.handle(
            raw_message,
            room_id=room_id,
            reply_mid=reply_mid,
            reply_dmid=reply_dmid,
            deliver=self._deliver_live_reply,
        )
        if handled:
            return

        await self._deliver_live_reply(raw_message, room_id, reply_mid, reply_dmid)

    async def _deliver_live_reply(
        self,
        raw_message: str,
        room_id: int,
        reply_mid: str,
        reply_dmid: str,
    ) -> None:
        """Deliver one already-orchestrated live reply through TTS/Live2D/danmu."""

        text, emotion, action = (
            self.adapter.live2d_manager.extract_json_emotion_from_text(raw_message)
        )
        text = _strip_emoji(text).strip()

        if text:
            text = (
                text.replace("\u200b", "")
                .replace("\\u200b", "")
                .replace("\\u200B", "")
                .replace("\ufeff", "")
                .strip()
            )

        text = self.adapter._filter_outgoing_text(text)
        if not text:
            return

        tts_enable = self.adapter.tts_manager.is_tts_enabled(room_id)

        self.logger.info(
            f"TTS Debug: room_id={room_id}, tts_enable={tts_enable}, tts_manager_active=True"
        )

        if tts_enable:
            self.adapter.tts_manager.buffer_tts_reply(
                room_id=room_id,
                text=text,
                reply_mid=reply_mid,
                reply_dmid=reply_dmid,
                emotion=emotion,
                action=action,
            )
            return

        if self.adapter.live2d_manager.controller:
            try:
                await self.adapter.live2d_manager.controller.on_start_replying()
                self.adapter.live2d_manager.execute_extracted_live2d_action(
                    emotion, action
                )
            except Exception as e:
                self.logger.error(f"Live2D reply hook error: {e}")

        await self._send_danmu(room_id, text, reply_mid or None, reply_dmid or None)

        if self.adapter.live2d_manager.controller:
            try:
                await self.adapter.live2d_manager.controller.on_reply_finished()
            except Exception as e:
                self.logger.error(f"Live2D reply hook error: {e}")

    async def _send_danmu(
        self,
        room_id: int,
        text: str,
        reply_mid: Optional[str],
        reply_dmid: Optional[str],
    ) -> None:
        text = self.adapter._filter_outgoing_text(text)

        max_len = BILIBILI_DANMU_MAX_LENGTH
        if self.adapter.tts_manager.is_tts_enabled(room_id):
            max_len = 9999

        segments = _split_bilibili_text(text, max_length=max_len)
        if not segments:
            self.logger.warning("Empty danmu after splitting")
            return
        self.logger.info(
            "Send danmu: room_id=%s reply_mid=%s reply_dmid=%s text=%s",
            room_id,
            reply_mid or "",
            reply_dmid or "",
            text,
        )
        for idx, segment in enumerate(segments):
            segment_reply_mid = reply_mid if idx == 0 else None
            segment_reply_dmid = reply_dmid if idx == 0 else None
            try:
                resp = await self.adapter.api.send_danmu(
                    room_id=room_id,
                    message=segment,
                    reply_mid=segment_reply_mid or None,
                    reply_dmid=segment_reply_dmid or None,
                )
                if (resp or {}).get("code") != 0:
                    self.logger.warning(f"Danmu send failed: {resp}")
                else:
                    dmid = None
                    data = (resp or {}).get("data", {})
                    if isinstance(data, dict):
                        dmid = data.get("dmid") or data.get("dmid_str")
                    self._remember_self_danmu(
                        room_id, str(dmid) if dmid else "", segment
                    )
                    self.logger.info("Danmu send ok")
            except Exception as exc:
                self.logger.error(f"Danmu send error: {exc}")
            if idx < len(segments) - 1:
                await asyncio.sleep(BILIBILI_DANMU_SEND_DELAY_SECONDS)

    def _remember_self_danmu(self, room_id: int, message_id: str, text: str) -> None:
        now = time.time()
        if message_id:
            room_cache = self.adapter._self_danmu_ids.setdefault(room_id, {})
            room_cache[message_id] = now
            if len(room_cache) > 500:
                for msg_id, ts in list(room_cache.items()):
                    if now - ts > 30:
                        room_cache.pop(msg_id, None)
        room_texts = self.adapter._self_danmu_texts.setdefault(room_id, [])
        if text:
            room_texts.append((text, now))
        if len(room_texts) > 200:
            self.adapter._self_danmu_texts[room_id] = [
                item for item in room_texts if now - item[1] <= 30
            ]

    def is_self_danmu(
        self,
        room_id: int,
        user_id: str,
        message_id: str,
        text: str,
    ) -> bool:
        if (
            (not self.config.live_allow_self_danmu)
            and self.config.dede_user_id
            and user_id
        ):
            if str(user_id) == str(self.config.dede_user_id):
                return True
        now = time.time()
        room_cache = self.adapter._self_danmu_ids.get(room_id, {})
        if message_id and message_id in room_cache:
            return True
        room_texts = self.adapter._self_danmu_texts.get(room_id, [])
        if text:
            window = 2.5 if len(text.strip()) <= 2 else 6.0
            for sent_text, ts in list(room_texts):
                if now - ts > 30:
                    continue
                if sent_text == text and (now - ts) <= window:
                    return True
        return False
