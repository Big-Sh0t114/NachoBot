import asyncio
import json
from loguru import logger
import time
from typing import Any, Dict, List, Optional, Tuple

from ncnk_message import (
    BaseMessageInfo,
    FormatInfo,
    GroupInfo,
    MessageBase,
    Seg,
    UserInfo,
)

ACCEPT_FORMAT = ["text", "reply", "command"]
COMMENT_REPLY_LIMIT = 10
COMMENT_LIMIT_FALLBACK_TEXT = "NachoBot有点口渴了哦，先休息一下啦~"

class CommentHandler:
    def __init__(self, config: Any, logger, adapter_ref: Any):
        self.config = config
        self.logger = logger
        self.adapter = adapter_ref
        
        self.comment_context: Dict[str, Dict[str, Any]] = {}
        self.comment_bootstrap_done = False
        self.comment_reply_state: Dict[Tuple[str, str], Dict[str, Any]] = {}

        self.reply_seen: List[str] = []
        self.reply_seen_set: set[str] = set()

    async def comment_notice_loop(self) -> None:
        while True:
            reply_items: List[Dict[str, Any]] = []
            at_items: List[Dict[str, Any]] = []
            try:
                reply_items = await self.adapter.api.get_reply_notifications(
                    self.config.comment_max_items
                )
            except Exception as exc:
                self.logger.warning(f"Reply notice fetch error: {exc}")
                self.logger.debug(f"Reply notice fetch error: {exc}")
            try:
                at_items = await self.adapter.api.get_at_notifications(
                    self.config.comment_max_items
                )
            except Exception as exc:
                self.logger.debug(f"At notice fetch error: {exc}")
            if reply_items or at_items:
                self.logger.info(
                    "Comment notices: reply={} at={}",
                    len(reply_items),
                    len(at_items),
                )
            else:
                self.logger.info("Comment notices: 0")
            if not self.comment_bootstrap_done:
                for item in reply_items:
                    self._track_notice_key(self._notice_key("reply", item))
                for item in at_items:
                    self._track_notice_key(self._notice_key("at", item))
                self.comment_bootstrap_done = True
            else:
                await self._handle_reply_notifications(reply_items, source="reply")
                await self._handle_reply_notifications(at_items, source="at")
            await asyncio.sleep(self.config.comment_poll_interval)

    def _notice_key(self, source: str, item: Dict[str, Any]) -> str:
        notify_id = str(item.get("id") or "")
        if not notify_id:
            return ""
        return f"{source}:{notify_id}"

    def _track_notice_key(self, notify_key: str) -> bool:
        if not notify_key or notify_key in self.reply_seen_set:
            return False
        self.reply_seen_set.add(notify_key)
        self.reply_seen.append(notify_key)
        if len(self.reply_seen) > 500:
            old = self.reply_seen.pop(0)
            self.reply_seen_set.discard(old)
        return True

    def _is_at_me(self, at_details: Any) -> bool:
        if not self.config.dede_user_id:
            return False
        if not isinstance(at_details, list):
            return False
        for detail in at_details:
            if not isinstance(detail, dict):
                continue
            if str(detail.get("mid") or "") == self.config.dede_user_id:
                return True
        return False

    @staticmethod
    def _build_comment_reply_target_from_item(
        comment_type: Any,
        comment_oid: Any,
        reply_item: Dict[str, Any],
    ) -> Optional[Tuple[int, int, Optional[int], Optional[int]]]:
        try:
            comment_type_int = int(comment_type)
            comment_oid_int = int(comment_oid)
        except (TypeError, ValueError):
            return None

        root = None
        parent = None
        for value in (
            reply_item.get("root_id"),
            reply_item.get("source_id"),
            reply_item.get("target_id"),
        ):
            if value not in (None, "", 0):
                try:
                    root = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        for value in (
            reply_item.get("source_id"),
            reply_item.get("target_id"),
            reply_item.get("root_id"),
        ):
            if value not in (None, "", 0):
                try:
                    parent = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        return comment_type_int, comment_oid_int, root, parent

    async def _handle_reply_notifications(
        self, items: List[Dict[str, Any]], source: str
    ) -> None:
        if not items:
            return
        for item in items:
            notify_id = str(item.get("id") or "")
            if not notify_id:
                continue
            notify_key = self._notice_key(source, item)
            if not self._track_notice_key(notify_key):
                continue

            user = item.get("user") or {}
            reply_item = item.get("item") or {}
            user_id = str(user.get("mid") or "")
            user_name = str(user.get("nickname") or user_id)
            if (
                self.config.comment_resolve_user_nickname
                and user_id
                and (not user_name or user_name == user_id)
            ):
                user_name = await self.adapter._resolve_user_nickname(user_id)
            business_id = reply_item.get("business_id")
            subject_id = reply_item.get("subject_id")
            content = (
                reply_item.get("source_content")
                or reply_item.get("target_reply_content")
                or reply_item.get("root_reply_content")
                or reply_item.get("title")
                or ""
            )
            at_details = reply_item.get("at_details") or []
            is_at_me = self._is_at_me(at_details)
            if source == "at" and self.config.dede_user_id and not is_at_me:
                self.logger.debug("At notice without bot mention: id={}", notify_id)
            group_id = f"comment:{business_id}:{subject_id}"
            self.remember_comment_context(
                group_id=group_id,
                comment_type=business_id,
                comment_oid=subject_id,
                root_id=reply_item.get("root_id"),
                source_id=reply_item.get("source_id"),
                target_id=reply_item.get("target_id"),
            )
            state_key = (group_id, user_id)
            state = self.comment_reply_state.get(
                state_key, {"count": 0, "silenced": False, "fallback_sent": False}
            )
            if state.get("silenced"):
                continue
            if state.get("count", 0) >= COMMENT_REPLY_LIMIT:
                if not state.get("fallback_sent"):
                    target = self._build_comment_reply_target_from_item(
                        business_id, subject_id, reply_item
                    )
                    if target:
                        await self.send_comment_reply_from_context(
                            target, COMMENT_LIMIT_FALLBACK_TEXT
                        )
                    else:
                        self.logger.warning(
                            "Comment fallback reply skipped: invalid target group_id={} user_id={}",
                            group_id,
                            user_id,
                        )
                    state["fallback_sent"] = True
                state["silenced"] = True
                self.comment_reply_state[state_key] = state
                continue
            now_ts = time.time()
            reply_time = float(item.get("reply_time") or now_ts)
            message_info = BaseMessageInfo(
                platform=self.config.platform,
                message_id=notify_id,
                time=now_ts,
                user_info=UserInfo(
                    platform=self.config.platform,
                    user_id=user_id,
                    user_nickname=user_name,
                ),
                group_info=GroupInfo(
                    platform=self.config.platform,
                    group_id=group_id,
                    group_name=group_id,
                ),
                format_info=FormatInfo(
                    content_format=["text"],
                    accept_format=ACCEPT_FORMAT,
                ),
                additional_config=self._build_comment_notice_config(
                    business_id=business_id,
                    subject_id=subject_id,
                    reply_item=reply_item,
                    source=source,
                    is_at_me=is_at_me,
                    reply_time=reply_time,
                ),
            )
            message = MessageBase(
                message_info=message_info,
                message_segment=Seg(type="text", data=str(content)),
                raw_message=json.dumps(item, ensure_ascii=True),
            )
            await self.adapter._send_to_nachobot(message)
            state["count"] = int(state.get("count", 0)) + 1
            self.comment_reply_state[state_key] = state

    def _build_comment_notice_config(
        self,
        business_id: Any,
        subject_id: Any,
        reply_item: Dict[str, Any],
        source: str,
        is_at_me: bool,
        reply_time: float,
    ) -> Dict[str, Any]:
        config: Dict[str, Any] = {
            "comment_type": business_id,
            "comment_oid": subject_id,
            "root_id": reply_item.get("root_id"),
            "source_id": reply_item.get("source_id"),
            "target_id": reply_item.get("target_id"),
            "uri": reply_item.get("uri"),
            "notice_source": source,
            "reply_time": reply_time,
        }
        if (
            self.config.comment_force_mention
            or source == "reply"
            or source == "at"
            or is_at_me
        ):
            config["is_mentioned"] = 1.0
        return config

    def remember_comment_context(
        self,
        group_id: str,
        comment_type: Optional[int],
        comment_oid: Optional[int],
        root_id: Any,
        source_id: Any,
        target_id: Any,
    ) -> None:
        if not group_id:
            return
        self.comment_context[group_id] = {
            "comment_type": comment_type,
            "comment_oid": comment_oid,
            "root_id": root_id,
            "source_id": source_id,
            "target_id": target_id,
            "ts": time.time(),
        }

    @staticmethod
    def _parse_comment_group_id(group_id: str) -> Optional[Tuple[int, int]]:
        if not group_id or not group_id.startswith("comment:"):
            return None
        parts = group_id.split(":")
        if len(parts) != 3:
            return None
        try:
            return int(parts[1]), int(parts[2])
        except ValueError:
            return None

    def resolve_comment_target(
        self, message: MessageBase
    ) -> Optional[Tuple[int, int, Optional[int], Optional[int]]]:
        group_info = message.message_info.group_info
        group_id = group_info.group_id if group_info else None
        if not group_id:
            return None
        parsed = self._parse_comment_group_id(group_id)
        if not parsed:
            return None
        comment_type, comment_oid = parsed
        context = self.comment_context.get(group_id, {})
        root_id = context.get("root_id")
        source_id = context.get("source_id")
        target_id = context.get("target_id")
        root = None
        parent = None
        for value in (root_id, source_id, target_id):
            if value not in (None, "", 0):
                try:
                    root = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        for value in (source_id, target_id, root_id):
            if value not in (None, "", 0):
                try:
                    parent = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        return comment_type, comment_oid, root, parent

    async def send_comment_reply_from_context(
        self,
        target: Tuple[int, int, Optional[int], Optional[int]],
        text: str,
    ) -> None:
        text = self.adapter._filter_outgoing_text(text)
        comment_type, oid, root_id, parent_id = target
        self.logger.info(
            "Send comment reply: type={} oid={} root={} parent={}",
            comment_type,
            oid,
            root_id,
            parent_id,
        )
        try:
            resp = await self.adapter.api.send_comment_reply(
                comment_type=comment_type,
                oid=oid,
                message=text,
                root=root_id,
                parent=parent_id,
            )
            if (resp or {}).get("code") != 0:
                self.logger.warning(f"Comment reply failed: {resp}")
            else:
                self.logger.info("Comment reply ok")
        except Exception as exc:
            self.logger.error(f"Comment reply error: {exc}")
