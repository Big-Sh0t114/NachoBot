import asyncio
import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ncnk_message import (
    BaseMessageInfo,
    FormatInfo,
    MessageBase,
    Seg,
    UserInfo,
)

from bili_src.core.config import PrivateSessionConfig
from bili_src.core.utils import (
    _decode_image_base64,
    _normalize_text,
    _strip_emoji,
    _URL_RE,
)
from bili_src.visual_policy import build_private_visual_policy

ACCEPT_FORMAT_PRIVATE = ["text", "image", "emoji", "reply", "command"]


class PrivateHandler:
    def __init__(self, config: Any, logger, adapter_ref: Any):
        self.config = config
        self.logger = logger
        self.adapter = adapter_ref

        self.dm_last_seqno: Dict[Tuple[int, int], int] = {}
        self.last_private_session: Optional[PrivateSessionConfig] = None
        self.private_session_by_group: Dict[str, PrivateSessionConfig] = {}
        self.auto_private_sessions: List[PrivateSessionConfig] = []
        self.auto_private_sessions_ts: float = 0.0

    async def private_message_loop(self) -> None:
        while True:
            try:
                await self._poll_private_messages()
            except Exception as exc:
                self.logger.warning(f"Private message loop error: {exc}")
            await asyncio.sleep(self.config.private_poll_interval)

    async def _get_private_sessions(self) -> List[PrivateSessionConfig]:
        sessions: Dict[Tuple[int, int], PrivateSessionConfig] = {
            (item.session_type, item.talker_id): item
            for item in self.config.private_sessions
        }
        if not self.config.private_auto_sessions:
            return list(sessions.values())

        now = time.time()
        refresh_seconds = max(5, self.config.private_auto_session_refresh_seconds)
        if (
            not self.auto_private_sessions
            or (now - self.auto_private_sessions_ts) >= refresh_seconds
        ):
            auto_sessions: Dict[Tuple[int, int], PrivateSessionConfig] = {}
            for session_type in self.config.private_auto_session_types:
                resp = await self.adapter.api.get_sessions(
                    session_type=session_type,
                    size=self.config.private_auto_session_size,
                )
                if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                    self.logger.warning(
                        "Session list failed: session_type=%s code=%s message=%s",
                        session_type,
                        resp.get("code"),
                        resp.get("message") or resp.get("msg"),
                    )
                    continue
                data = (resp or {}).get("data", {})
                session_list = data.get("session_list") or []
                if not isinstance(session_list, list):
                    continue
                for item in session_list:
                    if not isinstance(item, dict):
                        continue
                    try:
                        talker_id = int(item.get("talker_id") or 0)
                        item_type = int(item.get("session_type") or session_type)
                    except (TypeError, ValueError):
                        continue
                    if item_type not in (1, 2):
                        continue
                    if not talker_id:
                        continue
                    auto_sessions[(item_type, talker_id)] = PrivateSessionConfig(
                        talker_id=talker_id,
                        session_type=item_type,
                    )
            self.auto_private_sessions = list(auto_sessions.values())
            self.auto_private_sessions_ts = now

        for item in self.auto_private_sessions:
            sessions.setdefault((item.session_type, item.talker_id), item)
        return list(sessions.values())

    async def _poll_private_messages(self) -> None:
        sessions = await self._get_private_sessions()
        if not sessions:
            self.logger.debug("Private polling: no sessions")
            return
        self.logger.debug("Private polling: %s sessions", len(sessions))
        for session in sessions:
            key = (session.session_type, session.talker_id)
            last_seqno = self.dm_last_seqno.get(key)
            resp = await self.adapter.api.fetch_session_msgs(
                talker_id=session.talker_id,
                session_type=session.session_type,
                size=20,
                begin_seqno=last_seqno,
            )
            if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                self.logger.warning(
                    "Private poll failed: talker_id=%s session_type=%s code=%s message=%s",
                    session.talker_id,
                    session.session_type,
                    resp.get("code"),
                    resp.get("message") or resp.get("msg"),
                )
            data = (resp or {}).get("data", {})
            messages = data.get("messages") or []
            max_seqno = data.get("max_seqno")
            if max_seqno is None:
                continue
            if last_seqno is None:
                self.dm_last_seqno[key] = int(max_seqno)
                continue
            if not messages:
                if int(max_seqno) > last_seqno:
                    self.dm_last_seqno[key] = int(max_seqno)
                continue
            await self._emit_private_messages(session=session, messages=messages)
            self.dm_last_seqno[key] = int(max_seqno)

    async def _emit_private_messages(
        self,
        session: PrivateSessionConfig,
        messages: Iterable[Dict[str, Any]],
    ) -> None:
        messages_list = list(messages)
        if messages_list:
            self.logger.info(
                "Private messages: talker_id=%s session_type=%s count=%s",
                session.talker_id,
                session.session_type,
                len(messages_list),
            )
        for msg in reversed(messages_list):
            sender_uid = str(msg.get("sender_uid") or "")
            if (
                sender_uid
                and self.config.dede_user_id
                and sender_uid == str(self.config.dede_user_id)
            ):
                continue
            if hasattr(self.adapter, "tts_manager") and self.adapter.tts_manager:
                self.adapter.tts_manager.reset_idle_timer()
            msg_type = int(msg.get("msg_type") or 0)
            content = msg.get("content")
            content_text = ""
            segment: Optional[Seg] = None
            content_format = ["text"]
            image_url = ""
            if msg_type in (2, 6):
                image_url = self._extract_private_image_url(content)
                if image_url:
                    image_base64 = await self._download_private_image(image_url)
                    if image_base64:
                        segment = Seg(type="image", data=image_base64)
                        content_format = ["image"]
                if segment is None:
                    content_text = (
                        self.parse_private_content(msg_type, content) or "[image]"
                    )
            else:
                content_text = self.parse_private_content(msg_type, content)

            if segment is None:
                if not content_text:
                    continue
                segment = Seg(type="text", data=content_text)
            message_id = str(
                msg.get("msg_key") or msg.get("msg_seqno") or uuid.uuid4().hex
            )
            now_ts = time.time()
            msg_time = float(msg.get("timestamp") or now_ts)
            group_id = f"dm:{session.session_type}:{session.talker_id}"
            self.remember_private_session(group_id, session)
            sender_name = await self.adapter._resolve_user_nickname(
                sender_uid or str(session.talker_id)
            )
            additional_config = {
                "session_type": session.session_type,
                "talker_id": session.talker_id,
                "msg_type": msg_type,
                "msg_seqno": msg.get("msg_seqno"),
                "message_time": msg_time,
            }
            if image_url:
                additional_config["image_url"] = image_url
            if segment.type == "image":
                additional_config["visual_policy"] = build_private_visual_policy(
                    self.config.private_visual.image
                )
            if self.config.private_force_mention:
                additional_config["is_mentioned"] = 1.0
            message_info = BaseMessageInfo(
                platform=self.config.platform,
                message_id=message_id,
                time=now_ts,
                user_info=UserInfo(
                    platform=self.config.platform,
                    user_id=sender_uid or str(session.talker_id),
                    user_nickname=sender_name,
                ),
                group_info=None,
                format_info=FormatInfo(
                    content_format=content_format,
                    accept_format=ACCEPT_FORMAT_PRIVATE,
                ),
                additional_config=additional_config,
            )
            message = MessageBase(
                message_info=message_info,
                message_segment=segment,
                raw_message=json.dumps(msg, ensure_ascii=False),
            )
            await self.adapter._send_to_nachobot(message)

    def remember_private_session(
        self, group_id: str, session: PrivateSessionConfig
    ) -> None:
        self.private_session_by_group[group_id] = session
        self.last_private_session = session

    def resolve_private_target(
        self, message: MessageBase
    ) -> Optional[PrivateSessionConfig]:
        group_info = message.message_info.group_info
        if group_info and group_info.group_id:
            parsed = self.parse_private_group_id(group_info.group_id)
            if parsed:
                return parsed
        additional = message.message_info.additional_config or {}
        talker_id = additional.get("talker_id")
        session_type = additional.get("session_type")
        if talker_id not in (None, "", 0):
            try:
                return PrivateSessionConfig(
                    talker_id=int(talker_id),
                    session_type=int(session_type or 1),
                )
            except (TypeError, ValueError):
                pass
        user_info = message.message_info.user_info
        if user_info and user_info.user_id:
            try:
                return PrivateSessionConfig(
                    talker_id=int(user_info.user_id),
                    session_type=int(session_type or 1),
                )
            except (TypeError, ValueError):
                pass
        return self.last_private_session

    @staticmethod
    def parse_private_group_id(group_id: str) -> Optional[PrivateSessionConfig]:
        if not group_id or not group_id.startswith("dm:"):
            return None
        parts = group_id.split(":")
        if len(parts) != 3:
            return None
        try:
            session_type = int(parts[1])
            talker_id = int(parts[2])
        except ValueError:
            return None
        return PrivateSessionConfig(talker_id=talker_id, session_type=session_type)

    @staticmethod
    def parse_private_content(msg_type: int, content: Any) -> str:
        if msg_type == 1 and content:
            text = ""
            if isinstance(content, dict):
                text = str(
                    content.get("content")
                    or content.get("text")
                    or content.get("title")
                    or ""
                )
            elif isinstance(content, str):
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        text = str(
                            data.get("content")
                            or data.get("text")
                            or data.get("title")
                            or ""
                        )
                    elif isinstance(data, str):
                        text = data
                except Exception:
                    text = content
            text = _normalize_text(text)
            return _strip_emoji(text).strip()
        if msg_type in (2, 6):
            return "[image]"
        return ""

    async def send_private_message(
        self, session: PrivateSessionConfig, text: str
    ) -> None:
        self.logger.info(
            "Send private message: talker_id=%s session_type=%s text=%s",
            session.talker_id,
            session.session_type,
            text,
        )
        try:
            safe_text = _normalize_text(text)
            safe_text = self.adapter._filter_outgoing_text(safe_text)
            if not safe_text:
                return
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await self.adapter.api.send_private_message(
                        talker_id=session.talker_id,
                        session_type=session.session_type,
                        message=safe_text,
                    )
                except Exception as exc:
                    if attempt < max_attempts:
                        self.logger.warning(
                            "Private message send failed (attempt %s/%s): %s",
                            attempt,
                            max_attempts,
                            exc,
                        )
                        await asyncio.sleep(0.6 * attempt)
                        continue
                    raise
                if (resp or {}).get("code") != 0:
                    self.logger.warning(f"Private message failed: {resp}")
                    return
                self.logger.info("Private message ok")
                return
        except Exception as exc:
            self.logger.error(f"Private message error: {exc}")

    async def send_private_image(
        self, session: PrivateSessionConfig, image_base64: str
    ) -> None:
        image_bytes, image_format = _decode_image_base64(image_base64)
        if not image_bytes:
            self.logger.warning("Private image send failed: invalid image data")
            return
        try:
            upload_resp = await self.adapter.api.upload_dynamic_image(
                image_bytes=image_bytes,
                image_format=image_format,
                category="daily",
            )
        except Exception as exc:
            self.logger.error("Private image upload error: %s", exc)
            return
        data = (upload_resp or {}).get("data", {})
        image_url = str(data.get("image_url") or "")
        if not image_url:
            self.logger.warning("Private image upload missing url: %s", upload_resp)
            return
        content: Dict[str, Any] = {"url": image_url}
        if data.get("image_height"):
            content["height"] = data.get("image_height")
        if data.get("image_width"):
            content["width"] = data.get("image_width")
        if data.get("img_size"):
            content["size"] = data.get("img_size")
        if image_format:
            content["imageType"] = image_format
        self.logger.info(
            "Send private image: talker_id=%s session_type=%s url=%s",
            session.talker_id,
            session.session_type,
            image_url,
        )
        try:
            resp = await self.adapter.api.send_private_image_message(
                talker_id=session.talker_id,
                session_type=session.session_type,
                content=content,
            )
            if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                self.logger.warning(f"Private image failed: {resp}")
            else:
                self.logger.info("Private image ok")
        except Exception as exc:
            self.logger.error(f"Private image error: {exc}")

    @staticmethod
    def _normalize_image_url(url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return f"https:{url}"
        return url

    def _extract_private_image_url(self, content: Any) -> str:
        image_keys = (
            "url",
            "image_url",
            "img_url",
            "image",
            "img",
            "src",
            "origin_url",
            "original_url",
            "preview",
            "cover",
            "thumb",
            "pic",
            "pic_url",
            "picture",
            "photo",
            "face",
            "raw_url",
        )

        def scan(value: Any) -> str:
            if isinstance(value, str):
                candidate = value.strip()
                if candidate.startswith(("http://", "https://", "//")):
                    return candidate
                match = _URL_RE.search(candidate)
                return match.group(0) if match else ""
            if isinstance(value, dict):
                for key in image_keys:
                    if key in value:
                        found = scan(value.get(key))
                        if found:
                            return found
                for item in value.values():
                    found = scan(item)
                    if found:
                        return found
            if isinstance(value, list):
                for item in value:
                    found = scan(item)
                    if found:
                        return found
            return ""

        content_value: Any = content
        if isinstance(content, str):
            trimmed = content.strip()
            if trimmed.startswith("{") or trimmed.startswith("["):
                try:
                    content_value = json.loads(trimmed)
                except json.JSONDecodeError:
                    content_value = content
        url = scan(content_value)
        return self._normalize_image_url(url)

    async def _download_private_image(self, url: str) -> Optional[str]:
        if not url:
            return None
        try:
            return await self.adapter.api.fetch_base64(url)
        except Exception as exc:
            self.logger.warning(
                "Private image download failed: url=%s error=%s", url, exc
            )
            return None

    async def handle_private_send(
        self, args: Dict[str, Any], message: Optional[MessageBase]
    ) -> None:
        raw_msg = str(args.get("message") or "")
        text, emotion, action = (
            self.adapter.live2d_manager.extract_json_emotion_from_text(raw_msg)
        )
        text = _strip_emoji(text).strip()

        self.adapter.live2d_manager.execute_extracted_live2d_action(emotion, action)

        if not text:
            return
        talker_id = args.get("talker_id")
        session_type = args.get("session_type")
        target = None
        if talker_id not in (None, "", 0):
            try:
                target = PrivateSessionConfig(
                    talker_id=int(talker_id),
                    session_type=int(session_type or 1),
                )
            except (TypeError, ValueError):
                target = None
        if target is None and message is not None:
            target = self.resolve_private_target(message)
        if target is None:
            self.logger.warning("Missing private message target")
            return
        await self.send_private_message(target, text)
