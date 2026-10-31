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
from ncnk_message.external_self_message import outbound_echo_registry

ACCEPT_FORMAT = ["text", "reply", "command"]
COMMENT_REPLY_LIMIT = 10
COMMENT_LIMIT_FALLBACK_TEXT = "NachoBot有点口渴了哦，先休息一下啦~"
COMMENT_DELIVERY_TARGET_KIND = "bilibili.comment"


def _register_echo(adapter: Any, peer_id: str) -> str | None:
    register = getattr(adapter, "_register_external_self_send", None)
    return register("group", peer_id) if callable(register) else None


def _resolve_echo(
    adapter: Any,
    token: str | None,
    message_id: Any = None,
    *,
    success: bool = False,
) -> None:
    resolve = getattr(adapter, "_resolve_external_self_send", None)
    if callable(resolve):
        resolve(token, message_id, success=success)


def _mark_echo_uncertain(adapter: Any, token: str | None) -> None:
    mark_uncertain = getattr(adapter, "_mark_external_self_send_uncertain", None)
    if callable(mark_uncertain):
        mark_uncertain(token)

class CommentHandler:
    def __init__(self, config: Any, logger, adapter_ref: Any):
        self.config = config
        self.logger = logger
        self.adapter = adapter_ref

        # Keep the legacy inspection surface for callers, but never use this
        # mutable per-video cache to route an asynchronous reply.
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

    def _untrack_notice_key(self, notify_key: str) -> None:
        if not notify_key or notify_key not in self.reply_seen_set:
            return
        self.reply_seen_set.discard(notify_key)
        try:
            self.reply_seen.remove(notify_key)
        except ValueError:
            pass

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
        comment_type_int = CommentHandler._positive_int(comment_type)
        comment_oid_int = CommentHandler._positive_int(comment_oid)
        if comment_type_int is None or comment_oid_int is None:
            return None

        root = None
        parent = None
        for value in (
            reply_item.get("root_id"),
            reply_item.get("source_id"),
            reply_item.get("target_id"),
        ):
            root = CommentHandler._positive_int(value)
            if root is not None:
                break
        for value in (
            reply_item.get("source_id"),
            reply_item.get("target_id"),
            reply_item.get("root_id"),
        ):
            parent = CommentHandler._positive_int(value)
            if parent is not None:
                break
        if root is None or parent is None:
            return None
        return comment_type_int, comment_oid_int, root, parent

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = int(value.strip())
            except ValueError:
                return None
        else:
            return None
        return parsed if parsed > 0 else None

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
            self_id = str(self.config.dede_user_id or "").strip()
            is_self = bool(self_id and user_id and user_id == self_id)
            user_name = str(user.get("nickname") or user_id)
            if (
                self.config.comment_resolve_user_nickname
                and user_id
                and (not user_name or user_name == user_id)
                and not is_self
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
            now_ts = time.time()
            try:
                reply_time = float(item.get("reply_time") or 0)
            except (TypeError, ValueError, OverflowError):
                reply_time = 0.0
            if is_self:
                if reply_time <= 0:
                    self._untrack_notice_key(notify_key)
                    continue
                native_comment_id = (
                    reply_item.get("rpid")
                    or reply_item.get("rpid_str")
                    or item.get("rpid")
                    or item.get("rpid_str")
                    or reply_item.get("id_str")
                )
                if native_comment_id in (None, ""):
                    continue
                own_message_id = str(native_comment_id)
                event_generation = getattr(
                    self.adapter, "_external_self_generation", "bilibili-adapter"
                )
                classification = await outbound_echo_registry.classify_event(
                    self.config.platform,
                    event_generation,
                    self_id,
                    "group",
                    group_id,
                    own_message_id,
                )
                if classification != "external":
                    continue
                passive_config = self._build_comment_notice_config(
                    notify_id=notify_id,
                    group_id=group_id,
                    business_id=business_id,
                    subject_id=subject_id,
                    reply_item=reply_item,
                    source=source,
                    is_at_me=is_at_me,
                    reply_time=reply_time,
                )
                passive_config.pop("delivery_target", None)
                passive_config.pop("is_mentioned", None)
                accepted = await self.adapter._forward_external_self_message(
                    self_id=self_id,
                    message_id=own_message_id,
                    route_kind="group",
                    peer_id=group_id,
                    original_timestamp=reply_time,
                    sender_name=user.get("nickname"),
                    generation=event_generation,
                    segments=[Seg(type="text", data=str(content))],
                    content_format=["text"],
                    additional_config=passive_config,
                    group_info=GroupInfo(
                        platform=self.config.platform,
                        group_id=group_id,
                        group_name=group_id,
                    ),
                    raw_message=json.dumps(item, ensure_ascii=True),
                )
                if not accepted:
                    self._untrack_notice_key(notify_key)
                continue
            if reply_time <= 0:
                reply_time = now_ts

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
                    notify_id=notify_id,
                    group_id=group_id,
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
        notify_id: str,
        group_id: str,
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
        target = self._build_comment_delivery_target(
            notify_id=notify_id,
            group_id=group_id,
            comment_type=business_id,
            comment_oid=subject_id,
            reply_item=reply_item,
        )
        if target is None:
            self.logger.warning(
                "Comment notice has no exact reply target: group_id={} message_id={}",
                group_id,
                notify_id,
            )
        else:
            config["delivery_target"] = target
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
        """Retain the legacy context snapshot without making it routable."""
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

    def _build_comment_delivery_target(
        self,
        notify_id: str,
        group_id: str,
        comment_type: Any,
        comment_oid: Any,
        reply_item: Dict[str, Any],
    ) -> dict[str, Any] | None:
        source_message_id = str(notify_id or "").strip()
        platform = str(getattr(self.config, "platform", "") or "").strip()
        target = self._build_comment_reply_target_from_item(
            comment_type, comment_oid, reply_item
        )
        parsed_group = self._parse_comment_group_id(group_id)
        if (
            not source_message_id
            or not platform
            or target is None
            or parsed_group != target[:2]
        ):
            return None

        target_type, target_oid, root, parent = target
        return {
            "kind": COMMENT_DELIVERY_TARGET_KIND,
            "platform": platform,
            "group_id": group_id,
            "scope": {"type": "group", "group_id": group_id},
            "comment_type": target_type,
            "oid": target_oid,
            "root": root,
            "parent": parent,
            "source_message_id": source_message_id,
        }

    @staticmethod
    def _parse_comment_group_id(group_id: str) -> Optional[Tuple[int, int]]:
        if not group_id or not group_id.startswith("comment:"):
            return None
        parts = group_id.split(":")
        if len(parts) != 3:
            return None
        comment_type = CommentHandler._positive_int(parts[1])
        comment_oid = CommentHandler._positive_int(parts[2])
        if comment_type is None or comment_oid is None:
            return None
        return comment_type, comment_oid

    def resolve_comment_target(
        self, message: MessageBase
    ) -> Optional[Tuple[int, int, Optional[int], Optional[int]]]:
        message_info = getattr(message, "message_info", None)
        group_info = getattr(message_info, "group_info", None)
        group_id = str(getattr(group_info, "group_id", "") or "")
        if not group_id:
            return None
        parsed = self._parse_comment_group_id(group_id)
        if not parsed:
            return None
        message_platform = str(getattr(message_info, "platform", "") or "")
        group_platform = str(getattr(group_info, "platform", "") or "")
        expected_platform = str(getattr(self.config, "platform", "") or "")
        if (
            not expected_platform
            or message_platform != expected_platform
            or (group_platform and group_platform != expected_platform)
        ):
            return None

        additional_config = getattr(message_info, "additional_config", None)
        if not isinstance(additional_config, dict):
            return None
        if "delivery_target" not in additional_config:
            return None
        target = additional_config.get("delivery_target")
        if not isinstance(target, dict):
            return None

        scope = target.get("scope")
        if (
            target.get("kind") != COMMENT_DELIVERY_TARGET_KIND
            or target.get("platform") != expected_platform
            or target.get("group_id") != group_id
            or not isinstance(scope, dict)
            or scope.get("type") != "group"
            or scope.get("group_id") != group_id
        ):
            return None

        comment_type = self._positive_int(target.get("comment_type"))
        comment_oid = self._positive_int(target.get("oid"))
        root = self._positive_int(target.get("root"))
        parent = self._positive_int(target.get("parent"))
        source_message_id = str(target.get("source_message_id") or "").strip()
        if (
            comment_type is None
            or comment_oid is None
            or root is None
            or parent is None
            or not source_message_id
            or (comment_type, comment_oid) != parsed
        ):
            return None

        explicit_source_id = additional_config.get("reply_to_message_id")
        if explicit_source_id not in (None, "") and str(explicit_source_id) != source_message_id:
            return None
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
        echo_token = _register_echo(
            self.adapter, f"comment:{comment_type}:{oid}"
        )
        try:
            resp = await self.adapter.api.send_comment_reply(
                comment_type=comment_type,
                oid=oid,
                message=text,
                root=root_id,
                parent=parent_id,
            )
            response_code = (resp or {}).get("code")
            if response_code != 0:
                if response_code is None:
                    _mark_echo_uncertain(self.adapter, echo_token)
                else:
                    _resolve_echo(self.adapter, echo_token, success=False)
                self.logger.warning(f"Comment reply failed: {resp}")
            else:
                data = (resp or {}).get("data") or {}
                reply_id = None
                if isinstance(data, dict):
                    reply_id = data.get("rpid") or data.get("rpid_str")
                    nested_reply = data.get("reply")
                    if reply_id is None and isinstance(nested_reply, dict):
                        reply_id = nested_reply.get("rpid") or nested_reply.get(
                            "rpid_str"
                        )
                if reply_id not in (None, ""):
                    _resolve_echo(
                        self.adapter, echo_token, reply_id, success=True
                    )
                else:
                    _mark_echo_uncertain(self.adapter, echo_token)
                self.logger.info("Comment reply ok")
        except asyncio.CancelledError:
            _mark_echo_uncertain(self.adapter, echo_token)
            raise
        except Exception as exc:
            _mark_echo_uncertain(self.adapter, echo_token)
            self.logger.error(f"Comment reply error: {exc}")
