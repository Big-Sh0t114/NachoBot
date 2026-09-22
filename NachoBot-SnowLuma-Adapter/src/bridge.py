from __future__ import annotations

import asyncio
import base64
import json
import time
import random
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout
from ncnk_message import (
    BaseMessageInfo,
    FormatInfo,
    GroupInfo,
    MessageBase,
    RouteConfig,
    Router,
    Seg,
    TargetConfig,
    UserInfo,
    build_system_event,
    build_system_event_route,
    get_core_token_from_env,
)

from .config import global_config
from .card_handler import parse_json_card, parse_share_card, parse_xml_card
from .log_safety import safe_endpoint, safe_exception, segment_summary
from .logger import custom_logger, logger
from .snowluma_client import SnowLumaClient

PLATFORM_API_REQUEST_TYPE = "platform_api_request"
PLATFORM_API_RESPONSE_TYPE = "platform_api_response"
_PLATFORM_API_VERSION = 1
_PLATFORM_API_OPERATIONS = {"get_platform_cookies", "like_qzone", "comment_qzone"}
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,256}$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_TID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_QQ_RE = re.compile(r"^[1-9][0-9]{0,19}$")

ACCEPT_FORMAT = [
    "text", "image", "emoji", "reply", "voice", "tts_text", "command", "voiceurl",
    "music", "videourl", "file", "imageurl", "forward", "video", "face",
]
VISUAL_TYPES = {"image", "emoji", "video"}


class SnowLumaBridge:
    def __init__(self) -> None:
        cfg = global_config.nachobot
        route_config = RouteConfig(
            route_config={
                cfg.platform_name: TargetConfig(
                    url=f"ws://{cfg.host}:{cfg.port}/ws",
                    token=get_core_token_from_env(),
                )
            }
        )
        self.router = Router(route_config, custom_logger)
        self.client = SnowLumaClient(self.handle_snowluma_event, self._platform_status)
        self._group_name_cache: dict[int, str] = {}
        self._member_cache: dict[tuple[int, int], dict[str, Any]] = {}
        self._http: ClientSession | None = None
        self._last_send_at = 0.0
        self._send_lock = asyncio.Lock()
        self._self_muted_groups: dict[int, float] = {}
        self._log_configuration_summary()

    def _log_configuration_summary(self) -> None:
        cfg = global_config
        chat = cfg.chat
        logger.info(
            "SnowLuma adapter configuration endpoint={} core_endpoint=ws://{}:{}/ws platform={} "
            "filter_enabled={} group_policy={} group_count={} private_policy={} private_count={} "
            "global_ban_count={} ban_qq_bot={} enable_poke={} raw_payload={} raw_outbound={}",
            cfg.snowluma.safe_ws_url(),
            cfg.nachobot.host,
            cfg.nachobot.port,
            cfg.nachobot.platform_name,
            chat.enable_chat_list_filter,
            chat.group_list_type,
            len(chat.group_list),
            chat.private_list_type,
            len(chat.private_list),
            len(chat.ban_user_id),
            chat.ban_qq_bot,
            chat.enable_poke,
            cfg.debug.raw_payload,
            cfg.debug.raw_outbound,
        )
        if chat.enable_chat_list_filter and chat.group_list_type == "whitelist" and not chat.group_list:
            logger.warning("SnowLuma chat admission fail-closed reason=empty_group_whitelist")
        if chat.enable_chat_list_filter and chat.private_list_type == "whitelist" and not chat.private_list:
            logger.warning("SnowLuma chat admission fail-closed reason=empty_private_whitelist")

    async def run(self) -> None:
        self.router.register_class_handler(self.handle_core_message)
        self.router.register_custom_message_handler(PLATFORM_API_REQUEST_TYPE, self.handle_platform_api_request)
        logger.info("SnowLuma bridge starting core_endpoint=ws://{}:{}/ws", global_config.nachobot.host, global_config.nachobot.port)
        await asyncio.gather(self.router.run(), self.client.run())

    async def stop(self) -> None:
        logger.info("SnowLuma bridge stopping")
        await self.client.stop()
        await self.router.stop()
        if self._http and not self._http.closed:
            await self._http.close()
        logger.info("SnowLuma bridge stopped")

    async def _platform_status(self, online: bool, account_id: str) -> None:
        logger.debug("Core platform_status handoff start online={} account_id={}", online, account_id or "unknown")
        try:
            await self.router.send_custom_message(
                platform=global_config.nachobot.platform_name,
                message_type_name="platform_status",
                message={
                    "platform": global_config.nachobot.platform_name,
                    "online": online,
                    "account_id": account_id,
                    "timestamp": time.time(),
                    "adapter": "snowluma",
                },
            )
            logger.info("Core platform_status handoff success online={} account_id={}", online, account_id or "unknown")
        except Exception as exc:
            logger.warning("Core platform_status handoff failed online={} error={}", online, safe_exception(exc))

    # ------------------------------------------------------------------
    # SnowLuma -> NachoBot Core
    # ------------------------------------------------------------------
    async def handle_snowluma_event(self, payload: dict[str, Any]) -> None:
        post_type = str(payload.get("post_type") or "")
        logger.debug(
            "SnowLuma event dispatch post_type={} notice_type={} message_type={} user_id={} group_id={} message_id={}",
            post_type or "missing",
            payload.get("notice_type") or "-",
            payload.get("message_type") or "-",
            payload.get("user_id") or "-",
            payload.get("group_id") or "-",
            payload.get("message_id") or "-",
        )
        try:
            if post_type == "message" or (not post_type and "message" in payload):
                await self._handle_inbound_message(payload)
            elif post_type == "notice":
                await self._handle_notice(payload)
            else:
                logger.debug("SnowLuma event ignored reason=unsupported_post_type post_type={}", post_type or "missing")
        except Exception as exc:
            logger.error("SnowLuma inbound event failed post_type={} error={}", post_type or "missing", safe_exception(exc))

    async def _allowed(
        self,
        user_id: int | None,
        group_id: int | None,
        *,
        ignore_bot: bool = False,
        ignore_global_list: bool = False,
        ignore_self_muted: bool = False,
    ) -> bool:
        """Apply the same user/group admission semantics as the dev NapCat adapter."""
        allowed, _ = await self._admission_check(
            user_id,
            group_id,
            ignore_bot=ignore_bot,
            ignore_global_list=ignore_global_list,
            ignore_self_muted=ignore_self_muted,
        )
        return allowed

    async def _admission_check(
        self,
        user_id: int | None,
        group_id: int | None,
        *,
        ignore_bot: bool = False,
        ignore_global_list: bool = False,
        ignore_self_muted: bool = False,
    ) -> tuple[bool, str]:
        """Return the NapCat-compatible decision and a stable rejection reason."""
        cfg = global_config.chat

        if group_id is None and user_id is None:
            logger.warning("SnowLuma admission rejected reason=missing_routing_identity route=unknown")
            return False, "missing_routing_identity"

        if group_id is not None and not ignore_self_muted:
            mute_end = self._self_muted_groups.get(group_id)
            if mute_end is not None:
                if time.time() < mute_end:
                    logger.warning(
                        "SnowLuma admission rejected reason=self_muted route=group group_id={}",
                        group_id,
                    )
                    return False, "self_muted"
                self._self_muted_groups.pop(group_id, None)
                logger.info("SnowLuma admission mute fence expired route=group group_id={}", group_id)

        if cfg.enable_chat_list_filter:
            if group_id is not None:
                hit = group_id in cfg.group_list
                if cfg.group_list_type == "whitelist":
                    allowed = hit
                    reason = "group_whitelist"
                elif cfg.group_list_type == "blacklist":
                    allowed = not hit
                    reason = "group_blacklist"
                else:
                    logger.warning(
                        "SnowLuma admission rejected reason=invalid_group_policy policy={} group_id={}",
                        cfg.group_list_type,
                        group_id,
                    )
                    return False, "invalid_group_policy"
            else:
                if user_id is None:
                    logger.warning("SnowLuma admission rejected reason=missing_routing_identity route=private")
                    return False, "missing_routing_identity"
                hit = user_id in cfg.private_list
                if cfg.private_list_type == "whitelist":
                    allowed = hit
                    reason = "private_whitelist"
                elif cfg.private_list_type == "blacklist":
                    allowed = not hit
                    reason = "private_blacklist"
                else:
                    logger.warning(
                        "SnowLuma admission rejected reason=invalid_private_policy policy={} user_id={}",
                        cfg.private_list_type,
                        user_id,
                    )
                    return False, "invalid_private_policy"
            if not allowed:
                logger.warning(
                    "SnowLuma admission rejected reason={} route={} user_id={} group_id={}",
                    reason,
                    "group" if group_id is not None else "private",
                    user_id or "-",
                    group_id or "-",
                )
                return False, reason

        if user_id is not None and user_id in cfg.ban_user_id and not ignore_global_list:
            logger.warning(
                "SnowLuma admission rejected reason=global_ban route={} user_id={} group_id={}",
                "group" if group_id is not None else "private",
                user_id,
                group_id or "-",
            )
            return False, "global_ban"

        if group_id is not None and user_id is not None and cfg.ban_qq_bot and not ignore_bot:
            member = await self._get_member_info(group_id, user_id)
            if member.get("is_robot") is True:
                logger.warning(
                    "SnowLuma admission rejected reason=qq_bot route=group user_id={} group_id={}",
                    user_id,
                    group_id,
                )
                return False, "qq_bot"

        logger.debug(
            "SnowLuma admission allowed route={} user_id={} group_id={} ignore_bot={} ignore_global_list={}",
            "group" if group_id is not None else "private",
            user_id or "-",
            group_id or "-",
            ignore_bot,
            ignore_global_list,
        )
        return True, "allowed"

    async def _handle_inbound_message(self, raw: dict[str, Any]) -> None:
        message_type = str(raw.get("message_type") or "")
        sender = raw.get("sender") if isinstance(raw.get("sender"), Mapping) else {}
        user_id = self._to_int(sender.get("user_id") or raw.get("user_id"))
        group_id = self._to_int(raw.get("group_id")) if message_type == "group" else None
        logger.debug(
            "SnowLuma inbound message start message_type={} user_id={} group_id={} message_id={} segments={}",
            message_type or "unknown",
            user_id or "-",
            group_id or "-",
            raw.get("message_id") or "-",
            len(raw.get("message")) if isinstance(raw.get("message"), list) else (1 if isinstance(raw.get("message"), str) else 0),
        )
        if not user_id:
            logger.warning("SnowLuma inbound message rejected reason=missing_routing_identity")
            return
        if not await self._allowed(user_id, group_id):
            return

        if group_id is not None:
            group_name = await self._get_group_name(group_id)
            group_info = GroupInfo(
                platform=global_config.nachobot.platform_name,
                group_id=group_id,
                group_name=group_name,
            )
        else:
            group_info = None

        user_info = UserInfo(
            platform=global_config.nachobot.platform_name,
            user_id=user_id,
            user_nickname=str(sender.get("nickname") or sender.get("nick") or user_id),
            user_cardname=str(sender.get("card") or "") or None,
        )

        raw_segments = raw.get("message")
        if isinstance(raw_segments, str):
            raw_segments = [{"type": "text", "data": {"text": raw_segments}}]
        if not isinstance(raw_segments, list):
            logger.warning("SnowLuma inbound message rejected reason=invalid_segment_container")
            return

        segments: list[Seg] = []
        additional_config: dict[str, Any] = {}
        conversion_start = time.monotonic()
        logger.debug("SnowLuma inbound segment conversion start count={}", len(raw_segments))
        for index, item in enumerate(raw_segments):
            kind = str(item.get("type") or "unknown") if isinstance(item, Mapping) else "invalid"
            logger.debug("SnowLuma inbound segment start index={} {}", index, segment_summary(kind, item.get("data") if isinstance(item, Mapping) else item))
            try:
                converted = await self._convert_inbound_segment(item, raw, additional_config)
            except Exception as exc:
                logger.warning(
                    "SnowLuma inbound segment failed index={} kind={} error={}",
                    index,
                    kind,
                    safe_exception(exc),
                )
                continue
            if converted is None:
                logger.warning("SnowLuma inbound segment degraded index={} kind={} reason=unsupported_or_empty", index, kind)
                continue
            if isinstance(converted, list):
                segments.extend(converted)
                output_count = len(converted)
            else:
                segments.append(converted)
                output_count = 1
            logger.debug("SnowLuma inbound segment result index={} kind={} output_count={}", index, kind, output_count)
        if not segments:
            logger.warning("SnowLuma inbound message dropped reason=no_convertible_segments")
            return

        if global_config.voice.use_tts:
            additional_config["allow_tts"] = True
        if self._contains_visual(segments):
            additional_config["visual_policy"] = global_config.visual.to_message_policy()

        message_info = BaseMessageInfo(
            platform=global_config.nachobot.platform_name,
            message_id=raw.get("message_id"),
            time=time.time(),
            user_info=user_info,
            group_info=group_info,
            template_info=None,
            format_info=FormatInfo(content_format=["text", "image", "emoji", "voice"], accept_format=ACCEPT_FORMAT),
            additional_config=additional_config,
        )
        message_base = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="seglist", data=segments),
            raw_message=(raw.get("raw_message") if isinstance(raw.get("raw_message"), str) else json.dumps(raw, ensure_ascii=False, default=str)),
        )
        logger.debug(
            "SnowLuma Core ordinary-message handoff start message_id={} route={} segment_count={} conversion_ms={}",
            raw.get("message_id") or "-",
            "group" if group_id is not None else "private",
            len(segments),
            int((time.monotonic() - conversion_start) * 1000),
        )
        ok = await self.router.send_message(message_base)
        if not ok:
            logger.error("SnowLuma Core ordinary-message handoff failed message_id={}", raw.get("message_id") or "-")
        else:
            logger.info(
                "SnowLuma Core ordinary-message handoff success message_id={} route={} segment_count={}",
                raw.get("message_id") or "-",
                "group" if group_id is not None else "private",
                len(segments),
            )

    async def _convert_inbound_segment(
        self, item: Any, raw: dict[str, Any], additional: dict[str, Any]
    ) -> Seg | list[Seg] | None:
        if not isinstance(item, Mapping):
            return None
        kind = str(item.get("type") or "")
        data = item.get("data") if isinstance(item.get("data"), Mapping) else {}
        if kind == "text":
            return Seg(type="text", data=str(data.get("text") or ""))
        if kind == "face":
            face_id = data.get("id")
            return Seg(type="text", data=f"[QQ表情:{face_id}]")
        if kind == "at":
            qq = str(data.get("qq") or "")
            group_id = self._to_int(raw.get("group_id"))
            if qq in {"all", "@all"}:
                return Seg(type="text", data="@全体成员")
            nickname = qq
            if group_id and qq.isdigit():
                member = await self._get_member_info(group_id, int(qq))
                nickname = str(member.get("card") or member.get("nickname") or qq)
            return Seg(type="text", data=f"@<{nickname}:{qq}>")
        if kind == "reply":
            reply_id = str(data.get("id") or data.get("message_id") or "").strip()
            if reply_id and reply_id != "0":
                additional["reply_message_id"] = reply_id
                return Seg(type="text", data=f"[回复消息:{reply_id}] ")
            return None
        if kind == "image":
            b64 = await self._media_base64(data, action="get_image")
            if not b64:
                logger.warning("SnowLuma inbound segment degraded kind=image reason=media_lookup_failed")
                return Seg(type="text", data="[图片]")
            subtype = data.get("sub_type", data.get("subType", 0))
            return Seg(type="image" if subtype in {0, "0", None} else "emoji", data=b64)
        if kind == "record":
            b64 = await self._record_base64(data)
            if not b64:
                logger.warning("SnowLuma inbound segment degraded kind=record reason=media_lookup_failed")
            return Seg(type="voice", data=b64) if b64 else Seg(type="text", data="[语音]")
        if kind == "video":
            file_info = {
                "name": data.get("file") or data.get("name") or "video.mp4",
                "url": data.get("url"),
                "path": data.get("path"),
                "file_size": data.get("file_size") or data.get("size"),
                "file_id": data.get("file_id"),
            }
            return Seg(type="video", data=file_info)
        if kind == "file":
            return Seg(
                type="file",
                data={
                    "name": data.get("name") or data.get("file") or "unknown_file",
                    "url": data.get("url"),
                    "path": data.get("path"),
                    "file_id": data.get("file_id"),
                    "size": data.get("size") or data.get("file_size"),
                },
            )
        if kind == "forward":
            logger.warning("SnowLuma inbound segment degraded kind=forward reason=placeholder")
            return Seg(type="text", data="[合并转发消息]")
        if kind == "json":
            # Card previews are attacker-controlled remote URLs.  Keep the
            # readable text and metadata, but do not add a new network-fetch
            # trigger at this adapter boundary.
            card_segments, card_metadata = await parse_json_card(item)
            if card_metadata:
                additional.setdefault("platform_card_payloads", []).append(card_metadata)
            return card_segments
        if kind == "xml":
            return parse_xml_card(item)
        if kind == "share":
            return parse_share_card(item)
        if kind:
            logger.warning("SnowLuma inbound segment degraded kind={} reason=unsupported_placeholder", kind)
            return Seg(type="text", data=f"[{kind}]")
        return None

    @staticmethod
    def _format_duration(duration_seconds: int) -> str:
        if duration_seconds < 60:
            return f"{duration_seconds}秒"
        minutes = duration_seconds // 60
        if minutes < 60:
            return f"{minutes}分钟"
        hours, remaining_minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours}小时{remaining_minutes}分钟" if remaining_minutes else f"{hours}小时"
        days, remaining_hours = divmod(hours, 24)
        return f"{days}天{remaining_hours}小时" if remaining_hours else f"{days}天"

    async def _notice_allowed(
        self,
        raw: dict[str, Any],
        group_id: int | None,
        user_id: int | None,
        *,
        ignore_self_muted: bool = False,
    ) -> bool:
        if group_id is not None:
            operator_id = self._to_int(raw.get("operator_id"))
            admission_user = operator_id or user_id
            # Some group notices have no concrete actor. Keep group policy, but
            # do not invent a sender solely for blacklist/bot checks.
            return await self._allowed(
                admission_user,
                group_id,
                ignore_bot=admission_user is None,
                ignore_global_list=admission_user is None,
                ignore_self_muted=ignore_self_muted,
            )
        return await self._allowed(user_id, None)

    async def _handle_notice(self, raw: dict[str, Any]) -> None:
        """Convert OneBot notices to the dev Core's senderless structured system_event contract."""
        notice_type = str(raw.get("notice_type") or "")
        sub_type = str(raw.get("sub_type") or "").strip()
        group_id = self._to_int(raw.get("group_id"))
        user_id = self._to_int(raw.get("user_id"))
        logger.debug(
            "SnowLuma inbound notice start notice_type={} sub_type={} user_id={} group_id={} operator_id={} target_id={}",
            notice_type or "missing",
            sub_type or "-",
            user_id or "-",
            group_id or "-",
            self._to_int(raw.get("operator_id")) or "-",
            self._to_int(raw.get("target_id")) or "-",
        )

        if notice_type in {"", "input_status", "heartbeat", "meta_event", "friend_recall"}:
            logger.debug("SnowLuma notice ignored reason=unsupported_or_internal notice_type={} sub_type={}", notice_type or "missing", sub_type or "-")
            return

        own_lift_while_muted = (
            notice_type == "group_ban"
            and sub_type == "lift_ban"
            and group_id is not None
            and group_id in self._self_muted_groups
            and str(raw.get("user_id") or "") == str(raw.get("self_id") or self.client.connected_account_id or "")
        )

        if notice_type == "notify" and sub_type == "poke":
            if not global_config.chat.enable_poke:
                logger.warning("SnowLuma notice rejected reason=poke_disabled notice_type=notify sub_type=poke")
                return
            if user_id is None:
                logger.warning("SnowLuma notice rejected reason=missing_routing_identity notice_type=notify sub_type=poke")
                return
            if not await self._notice_allowed(raw, group_id, user_id):
                return
            handled, meta = await self._handle_poke_notice(raw, group_id, user_id)
        elif group_id is not None:
            if not await self._notice_allowed(
                raw, group_id, user_id, ignore_self_muted=own_lift_while_muted
            ):
                return
            if notice_type == "group_ban" and sub_type in {"ban", "lift_ban"}:
                handled, meta = await self._handle_group_ban_notice(raw, group_id, sub_type)
            else:
                handled, meta = await self._handle_generic_group_notice(raw, group_id)
        else:
            logger.warning(
                "SnowLuma notice ignored reason=private_notice_unsupported notice_type={} sub_type={}",
                notice_type or "missing",
                sub_type or "-",
            )
            return

        if handled is None or meta is None:
            logger.debug(
                "SnowLuma notice ignored reason=unsupported_notice_payload notice_type={} sub_type={}",
                notice_type,
                sub_type or "-",
            )
            return

        group_info: GroupInfo | None = None
        if group_id is not None:
            group_info = GroupInfo(
                platform=global_config.nachobot.platform_name,
                group_id=group_id,
                group_name=await self._get_group_name(group_id),
            )

        try:
            system_event = build_system_event(
                meta["type"],
                actor=meta.get("actor"),
                target=meta.get("target"),
                data=meta.get("data") or {},
            )
        except (KeyError, ValueError) as exc:
            logger.warning("SnowLuma notice rejected reason=invalid_system_event_contract error={}", safe_exception(exc))
            return

        additional_config: dict[str, Any] = {
            "target_id": raw.get("target_id"),
            "system_event": system_event,
        }
        if group_info is None and meta.get("system_event_route") is not None:
            additional_config["system_event_route"] = meta["system_event_route"]

        message_info = BaseMessageInfo(
            platform=global_config.nachobot.platform_name,
            message_id="notice",
            time=time.time(),
            # dev Core requires structured system events to be senderless.
            user_info=None,
            group_info=group_info,
            template_info=None,
            format_info=FormatInfo(content_format=["text", "notify"], accept_format=ACCEPT_FORMAT),
            additional_config=additional_config,
        )

        # Match the dev NapCat adapter's optional low-latency poke response.
        self_id = str(raw.get("self_id") or self.client.connected_account_id or "")
        target_id = str(raw.get("target_id") or "")
        fast_poke_eligible = system_event.get("type") == "qq.poke" and self_id and target_id == self_id
        if fast_poke_eligible and random.random() < 0.5:
            result: dict[str, Any] = {"triggered": True, "result": "pending"}
            try:
                params: dict[str, Any] = {"user_id": user_id}
                if group_id is not None:
                    params["group_id"] = group_id
                response = await asyncio.wait_for(self.client.call_action("send_poke", params), timeout=1.0)
                result["result"] = "failed" if self.client.action_error(response) else "success"
            except asyncio.TimeoutError:
                result["result"] = "timeout"
                logger.warning("SnowLuma fast_poke action timeout group_id={} user_id={}", group_id or "-", user_id)
            except Exception as exc:
                result["result"] = "error"
                logger.warning("SnowLuma fast_poke action failed group_id={} user_id={} error={}", group_id or "-", user_id, safe_exception(exc))
            logger.debug("SnowLuma fast_poke result={} group_id={} user_id={}", result["result"], group_id or "-", user_id)
            system_event["data"]["fast_poke"] = result

        try:
            raw_json = json.dumps(raw, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            logger.warning("SnowLuma notice rejected reason=unserializable_payload")
            return

        message_base = MessageBase(
            message_info=message_info,
            message_segment=handled,
            raw_message=raw_json,
        )

        if own_lift_while_muted and group_id is not None:
            self._self_muted_groups.pop(group_id, None)

        ok = await self.router.send_message(message_base)
        if not ok:
            logger.error(
                "SnowLuma Core system_event handoff failed type={} group_id={} route={}",
                system_event.get("type"),
                group_id or "-",
                "group" if group_id is not None else "private",
            )
        else:
            logger.info(
                "SnowLuma Core system_event handoff success type={} group_id={} route={} senderless=true",
                system_event.get("type"),
                group_id or "-",
                "group" if group_id is not None else "private",
            )

    async def _handle_poke_notice(
        self, raw: dict[str, Any], group_id: int | None, user_id: int
    ) -> tuple[Seg | None, dict[str, Any] | None]:
        self_id = str(raw.get("self_id") or self.client.connected_account_id or "")
        target_id_int = self._parse_int(raw.get("target_id"))
        if target_id_int is None:
            return None, None
        target_id = str(target_id_int)

        if str(user_id) == self_id:
            # Bot 自身发起的戳一戳不是外部环境事件。
            return None, None

        if group_id is not None:
            actor_info = await self._get_member_info(group_id, user_id)
        else:
            actor_info = await self._get_stranger_info(user_id)
        actor_nick = str(actor_info.get("nickname") or user_id)
        actor_card = str(actor_info.get("card") or "") or None
        actor_name = actor_card or actor_nick

        target_name: str
        display_name: str
        if target_id == self_id:
            self_info = await self._get_self_info()
            target_name = str(self_info.get("nickname") or "NachoBot")
            display_name = ""
        elif group_id is not None:
            target_info = await self._get_member_info(group_id, target_id_int)
            target_name = str(target_info.get("card") or target_info.get("nickname") or target_id)
            display_name = actor_name
        else:
            return None, None

        raw_info = raw.get("raw_info") if isinstance(raw.get("raw_info"), list) else []
        first_txt, second_txt = "戳了戳", ""
        try:
            first_txt = str(raw_info[2].get("txt", first_txt))
            second_txt = str(raw_info[4].get("txt", ""))
        except Exception:
            pass

        text = f"{display_name}{first_txt}{target_name}{second_txt}（这是QQ的一个功能，用于提及某人，但没那么明显）"
        meta: dict[str, Any] = {
            "type": "qq.poke",
            "actor": {"user_id": str(user_id), "name": actor_name},
            "target": {"user_id": target_id, "name": target_name},
            "data": {
                "group_id": str(group_id) if group_id is not None else None,
                "raw_info": raw_info,
            },
        }
        if group_id is None:
            meta["system_event_route"] = build_system_event_route(
                global_config.nachobot.platform_name,
                user_id=str(user_id),
                nickname=actor_nick,
                cardname=actor_card,
            )
        return Seg(type="text", data=text), meta

    async def _handle_group_ban_notice(
        self, raw: dict[str, Any], group_id: int, sub_type: str
    ) -> tuple[Seg | None, dict[str, Any] | None]:
        operator_id = self._parse_int(raw.get("operator_id"))
        user_id = self._parse_int(raw.get("user_id"))
        if operator_id is None or user_id is None:
            return None, None

        operator_info = await self._get_member_info(group_id, operator_id)
        operator_name = str(operator_info.get("card") or operator_info.get("nickname") or operator_id)
        target: dict[str, str] | None = None

        if sub_type == "ban":
            duration_raw = raw.get("duration")
            if isinstance(duration_raw, bool) or not isinstance(duration_raw, (int, float)):
                return None, None
            duration = int(duration_raw)
            if user_id == 0:
                text = f"{operator_name}开启了全体禁言"
            else:
                target_info = await self._get_member_info(group_id, user_id)
                target_name = str(target_info.get("card") or target_info.get("nickname") or user_id)
                target = {"user_id": str(user_id), "name": target_name}
                text = f"{operator_name}将{target_name}禁言了{self._format_duration(duration)}"
                self_id = str(raw.get("self_id") or self.client.connected_account_id or "")
                if str(user_id) == self_id:
                    self._self_muted_groups[group_id] = time.time() + max(0, duration)
            return Seg(type="text", data=text), {
                "type": "qq.group_ban",
                "actor": {"user_id": str(operator_id), "name": operator_name},
                "target": target,
                "data": {
                    "duration": duration,
                    "group_id": str(group_id),
                    "all_members": user_id == 0,
                },
            }

        if user_id == 0:
            text = f"{operator_name}关闭了全体禁言"
        else:
            target_info = await self._get_member_info(group_id, user_id)
            target_name = str(target_info.get("card") or target_info.get("nickname") or user_id)
            target = {"user_id": str(user_id), "name": target_name}
            text = f"{operator_name}解除了{target_name}的禁言"
        return Seg(type="text", data=text), {
            "type": "qq.group_lift_ban",
            "actor": {"user_id": str(operator_id), "name": operator_name},
            "target": target,
            "data": {"group_id": str(group_id), "all_members": user_id == 0},
        }

    async def _handle_generic_group_notice(
        self, raw: dict[str, Any], group_id: int
    ) -> tuple[Seg | None, dict[str, Any] | None]:
        notice_type = str(raw.get("notice_type") or "unknown")
        sub_type = str(raw.get("sub_type") or "").strip()
        if notice_type in {"input_status", "heartbeat", "meta_event"}:
            return None, None

        target_id = self._to_int(raw.get("user_id")) or self._to_int(raw.get("target_id"))
        actor_id = self._to_int(raw.get("operator_id"))
        target_info = await self._get_member_info(group_id, target_id) if target_id else {}
        actor_info = await self._get_member_info(group_id, actor_id) if actor_id else {}
        target_name = (
            str(target_info.get("card") or target_info.get("nickname") or target_id)
            if target_id else None
        )
        actor_name = (
            str(actor_info.get("card") or actor_info.get("nickname") or actor_id)
            if actor_id else None
        )
        actor = {"user_id": str(actor_id), "name": actor_name} if actor_id else None
        target = {"user_id": str(target_id), "name": target_name} if target_id else None
        display_target = target_name or "群成员"

        event_type = f"qq.{notice_type}" + (f".{sub_type}" if sub_type else "")
        if notice_type == "group_admin":
            text = (
                f"{display_target}被设为群管理员" if sub_type == "set" else
                f"{display_target}被取消群管理员" if sub_type == "unset" else
                f"{display_target}的群管理员状态发生变化"
            )
        elif notice_type == "group_increase":
            text = f"{display_target}加入了群聊"
        elif notice_type == "group_decrease":
            if sub_type == "kick_me":
                text = "NachoBot被移出了群聊"
            elif sub_type == "kick":
                text = f"{display_target}被移出了群聊"
            else:
                text = f"{display_target}离开了群聊"
        elif notice_type == "group_card":
            card_new = str(raw.get("card_new") or raw.get("new_card") or "").strip()
            text = f"{display_target}的群名片变更为{card_new}" if card_new else f"{display_target}的群名片发生了变化"
        elif notice_type == "notify" and sub_type == "honor":
            honor = str(raw.get("honor_type") or raw.get("title") or "群荣誉").strip()
            text = f"{display_target}获得了{honor}"
        elif "title" in notice_type or sub_type in {"title", "special_title"}:
            title = str(raw.get("title") or raw.get("special_title") or "").strip()
            text = f"{display_target}的群头衔变更为{title}" if title else f"{display_target}的群头衔发生了变化"
        else:
            label = notice_type if not sub_type else f"{notice_type}.{sub_type}"
            text = f"{target_name}触发了{label}通知" if target_name else f"群内发生了{label}通知"

        return Seg(type="text", data=text), {
            "type": event_type,
            "actor": actor,
            "target": target,
            "data": {
                "group_id": str(group_id),
                "notice_type": notice_type,
                "sub_type": sub_type or None,
                "raw_event": raw,
            },
        }

    # ------------------------------------------------------------------
    # NachoBot Core -> SnowLuma
    # ------------------------------------------------------------------
    async def handle_platform_api_request(self, raw_message: dict[str, Any]) -> None:
        """Handle the small, allowlisted capability API exposed by Core."""
        content = raw_message.get("content") if isinstance(raw_message, Mapping) else None
        if not isinstance(content, Mapping):
            logger.warning("SnowLuma platform API request rejected reason=missing_content")
            return
        request_id = content.get("request_id")
        operation = content.get("operation")
        platform = content.get("platform")
        params = content.get("params")
        request_label = request_id[:8] if isinstance(request_id, str) else "invalid"
        logger.debug(
            "SnowLuma platform API request received operation={} request_id={}",
            operation if isinstance(operation, str) else "invalid",
            request_label,
        )
        outer_platform = raw_message.get("platform")
        if (
            content.get("version") != _PLATFORM_API_VERSION
            or not isinstance(request_id, str)
            or not _REQUEST_ID_RE.fullmatch(request_id)
            or operation not in _PLATFORM_API_OPERATIONS
            or outer_platform not in (None, global_config.nachobot.platform_name)
            or platform != global_config.nachobot.platform_name
            or not isinstance(params, Mapping)
        ):
            logger.warning(
                "SnowLuma platform API request rejected operation={} request_id={} reason=invalid_envelope",
                operation if isinstance(operation, str) else "invalid",
                request_label,
            )
            if (
                isinstance(request_id, str)
                and _REQUEST_ID_RE.fullmatch(request_id)
                and operation in _PLATFORM_API_OPERATIONS
                and outer_platform in (None, global_config.nachobot.platform_name)
            ):
                await self._send_platform_api_response(
                    request_id, operation, "error", "invalid_request"
                )
            return

        try:
            if operation == "get_platform_cookies":
                if set(params) != {"domain"} or not isinstance(params["domain"], str):
                    await self._send_platform_api_response(request_id, operation, "error", "invalid_request")
                    return
                domain = params["domain"].strip().lower()
                if not domain or len(domain) > 253 or not _DOMAIN_RE.fullmatch(domain):
                    await self._send_platform_api_response(request_id, operation, "error", "invalid_request")
                    return
                response = await self.client.call_action("get_cookies", {"domain": domain})
                data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
                result = {"cookies": data.get("cookies")} if isinstance(data.get("cookies"), str) else None
            elif operation == "like_qzone":
                if set(params) != {"tid", "target_uin", "abstime"}:
                    await self._send_platform_api_response(request_id, operation, "error", "invalid_request")
                    return
                tid = params["tid"]
                target_uin = params["target_uin"]
                abstime = params["abstime"]
                if (
                    not isinstance(tid, str)
                    or not _TID_RE.fullmatch(tid)
                    or isinstance(target_uin, bool)
                    or not isinstance(target_uin, int)
                    or not _QQ_RE.fullmatch(str(target_uin))
                    or isinstance(abstime, bool)
                    or not isinstance(abstime, int)
                    or abstime < 0
                    or abstime > 2**63 - 1
                ):
                    await self._send_platform_api_response(request_id, operation, "error", "invalid_request")
                    return
                response = await self.client.call_action("like_qzone", dict(params))
                result = {"success": True}
            else:
                if set(params) != {"tid", "target_uin", "content"}:
                    await self._send_platform_api_response(request_id, operation, "error", "invalid_request")
                    return
                tid = params["tid"]
                comment = params["content"]
                target_uin = params["target_uin"]
                if (
                    not isinstance(tid, str)
                    or not _TID_RE.fullmatch(tid)
                    or isinstance(target_uin, bool)
                    or not isinstance(target_uin, int)
                    or not _QQ_RE.fullmatch(str(target_uin))
                    or not isinstance(comment, str)
                    or not comment.strip()
                    or len(comment) > 3000
                ):
                    await self._send_platform_api_response(request_id, operation, "error", "invalid_request")
                    return
                response = await self.client.call_action("comment_qzone", dict(params))
                result = {"success": True}

            error = self.client.action_error(response)
            if error or result is None:
                await self._send_platform_api_response(request_id, operation, "error", "upstream_error")
            else:
                await self._send_platform_api_response(request_id, operation, "ok", data=result)
                logger.info(
                    "SnowLuma platform API request succeeded operation={} request_id={}",
                    operation,
                    request_label,
                )
        except Exception as exc:
            logger.warning("SnowLuma platform API operation failed operation={} error={}", operation, safe_exception(exc))
            await self._send_platform_api_response(request_id, operation, "error", "upstream_error")

    async def _send_platform_api_response(
        self,
        request_id: str,
        operation: str,
        status: str,
        error_code: str | None = None,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        response: dict[str, Any] = {
            "version": _PLATFORM_API_VERSION,
            "request_id": request_id,
            "operation": operation,
            "platform": global_config.nachobot.platform_name,
            "status": status,
        }
        if status == "ok" and data is not None:
            response["data"] = data
        elif status == "error":
            response["error"] = {"code": error_code or "upstream_error"}
        sent = await self.router.send_custom_message(
            platform=global_config.nachobot.platform_name,
            message_type_name=PLATFORM_API_RESPONSE_TYPE,
            message=response,
        )
        if not sent:
            logger.error(
                "SnowLuma platform API response send failed operation={} request_id={}",
                operation,
                request_id[:8],
            )

    async def handle_core_message(self, raw_message_base_dict: dict[str, Any]) -> None:
        logger.debug("SnowLuma Core outbound dispatch start keys={}", ",".join(sorted(raw_message_base_dict)) or "-")
        try:
            message_base = MessageBase.from_dict(raw_message_base_dict)
            seg = message_base.message_segment
            logger.debug(
                "SnowLuma Core outbound classified segment_type={} route={} group_id={} user_id={}",
                seg.type,
                "group" if message_base.message_info.group_info is not None else "private" if message_base.message_info.user_info is not None else "unknown",
                message_base.message_info.group_info.group_id if message_base.message_info.group_info is not None else "-",
                message_base.message_info.user_info.user_id if message_base.message_info.user_info is not None else "-",
            )
            if seg.type == "command":
                await self._handle_command(message_base)
            else:
                await self._send_normal(message_base)
        except Exception as exc:
            logger.error("SnowLuma Core outbound dispatch failed error={}", safe_exception(exc))

    async def _handle_command(self, message_base: MessageBase) -> None:
        data = message_base.message_segment.data or {}
        if not isinstance(data, Mapping):
            return
        name = str(data.get("name") or "")
        args = dict(data.get("args") or {})
        group_info = message_base.message_info.group_info
        action, params = self._map_command(name, args, group_info)
        logger.debug("SnowLuma Core command action start name={} action={} param_keys={}", name or "unknown", action, ",".join(sorted(params)) or "-")
        response = await self.client.call_action(action, params)
        error = self.client.action_error(response)
        if error:
            logger.warning("SnowLuma Core command action failed action={} error={}", action, error)
        else:
            logger.info("SnowLuma Core command action success action={}", action)

    def _map_command(self, name: str, args: dict[str, Any], group_info: GroupInfo | None) -> tuple[str, dict[str, Any]]:
        gid = self._to_int(group_info.group_id) if group_info else None
        if name == "GROUP_BAN":
            return "set_group_ban", {"group_id": gid, "user_id": int(args["qq_id"]), "duration": int(args["duration"])}
        if name == "GROUP_WHOLE_BAN":
            return "set_group_whole_ban", {"group_id": gid, "enable": bool(args["enable"])}
        if name == "GROUP_KICK":
            return "set_group_kick", {"group_id": gid, "user_id": int(args["qq_id"]), "reject_add_request": False}
        if name == "SEND_POKE":
            return "send_poke", {"group_id": gid, "user_id": int(args["qq_id"])}
        if name == "DELETE_MSG":
            return "delete_msg", {"message_id": int(args["message_id"])}
        if name == "AI_VOICE_SEND":
            return "send_group_ai_record", {"group_id": gid, "text": args["text"], "character": args["character"]}
        if name == "MESSAGE_LIKE":
            # SnowLuma's NapCat-parity action name is set_msg_emoji_like.
            # emoji_id is typed as string by the SnowLuma SDK, so normalize it here.
            return "set_msg_emoji_like", {
                "message_id": int(args["message_id"]),
                "emoji_id": str(args["emoji_id"]),
                "set": True,
            }
        if name == "SET_GROUP_TITLE":
            return "set_group_special_title", {
                "group_id": gid,
                "user_id": int(args["qq_id"]),
                "special_title": str(args.get("title", ""))[:6],
            }
        # Forward-compatible escape hatch: command name may already be an action.
        if name:
            return name, args
        raise ValueError("command name 为空")

    async def _send_normal(self, message_base: MessageBase) -> None:
        info = message_base.message_info
        group_info = info.group_info
        user_info = info.user_info
        segments = self._outbound_segments(message_base.message_segment)
        uploads = self._extract_file_uploads(message_base.message_segment)
        forward_nodes = self._extract_forward_nodes(message_base.message_segment)
        logger.debug(
            "SnowLuma outbound segment conversion result segments={} uploads={} forward_nodes={}",
            len(segments),
            len(uploads),
            len(forward_nodes),
        )
        if group_info is not None:
            target_id = self._to_int(group_info.group_id)
            action = "send_group_msg"
            params = {"group_id": target_id, "message": segments}
            upload_action = "upload_group_file"
            upload_target_key = "group_id"
            forward_action = "send_group_forward_msg"
        elif user_info is not None:
            target_id = self._to_int(user_info.user_id)
            action = "send_private_msg"
            params = {"user_id": target_id, "message": segments}
            upload_action = "upload_private_file"
            upload_target_key = "user_id"
            forward_action = "send_private_forward_msg"
        else:
            logger.error("SnowLuma outbound rejected reason=missing_routing_identity")
            return

        actual_id = None
        if segments:
            async with self._send_lock:
                delay = global_config.send.min_interval_sec - (time.monotonic() - self._last_send_at)
                if delay > 0:
                    logger.debug("SnowLuma outbound send throttle delay_sec={:.3f} action={}", delay, action)
                    await asyncio.sleep(delay)
                logger.debug("SnowLuma outbound action start action={} target_id={} segment_count={}", action, target_id or "-", len(segments))
                response = await self.client.call_action(action, params)
                self._last_send_at = time.monotonic()
            error = self.client.action_error(response)
            if error:
                logger.warning("SnowLuma outbound action failed action={} error={}", action, error)
                return
            data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
            actual_id = data.get("message_id")
            if actual_id is not None:
                await self._echo_message_id(message_base, str(actual_id))
            logger.info("SnowLuma outbound action success action={} message_id={} segment_count={}", action, actual_id or "-", len(segments))

        if forward_nodes:
            response = await self.client.call_action(
                forward_action,
                {upload_target_key: target_id, "messages": forward_nodes},
            )
            error = self.client.action_error(response)
            if error:
                logger.warning("SnowLuma outbound forward failed action={} node_count={} error={}", forward_action, len(forward_nodes), error)
            else:
                forward_data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
                forward_id = forward_data.get("message_id")
                if actual_id is None and forward_id is not None:
                    await self._echo_message_id(message_base, str(forward_id))
                logger.info("SnowLuma outbound forward success action={} message_id={} node_count={}", forward_action, forward_id or "-", len(forward_nodes))

        for upload in uploads:
            upload_params = {upload_target_key: target_id, **upload}
            response = await self.client.call_action(upload_action, upload_params)
            error = self.client.action_error(response)
            if error:
                logger.warning("SnowLuma outbound file upload failed action={} error={}", upload_action, error)
            else:
                logger.info("SnowLuma outbound file upload success action={} size={} name_present={}", upload_action, self._file_size(upload.get("file")), bool(upload.get("name")))

        if not segments and not uploads and not forward_nodes:
            logger.warning("SnowLuma outbound dropped reason=no_sendable_segments")

    def _outbound_segments(self, root: Seg, in_forward: bool = False) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        items = root.data if root.type == "seglist" and isinstance(root.data, list) else [root]
        for item in items:
            if isinstance(item, dict):
                try:
                    item = Seg.from_dict(item)
                except Exception:
                    continue
            if not isinstance(item, Seg):
                continue
            kind, data = item.type, item.data
            logger.debug("SnowLuma outbound segment convert start {}", segment_summary(kind, data))
            if kind == "text" and data:
                out.append({"type": "text", "data": {"text": str(data)}})
            elif kind == "reply" and data is not None:
                target = data.get("target_message_id") if isinstance(data, Mapping) else data
                if str(target).lstrip("-").isdigit() and int(target) != 0:
                    out.append({"type": "reply", "data": {"id": str(target)}})
                else:
                    logger.warning("SnowLuma outbound segment degraded kind=reply reason=invalid_target")
            elif kind == "face" and data is not None:
                try:
                    out.append({"type": "face", "data": {"id": int(data)}})
                except (TypeError, ValueError):
                    logger.warning("SnowLuma outbound segment degraded kind=face reason=invalid_id")
            elif kind == "image" and data:
                out.append({"type": "image", "data": {"file": self._file_ref(data), "subType": 0}})
            elif kind == "emoji" and data:
                out.append({"type": "image", "data": {"file": self._file_ref(data), "subType": 1, "summary": "[动画表情]"}})
            elif kind == "imageurl" and data:
                out.append({"type": "image", "data": {"file": self._mapping_ref(data)}})
            elif kind == "voice" and data and global_config.voice.use_tts:
                out.append({"type": "record", "data": {"file": self._file_ref(data)}})
            elif kind == "voice" and data:
                logger.warning("SnowLuma outbound segment degraded kind=voice reason=tts_disabled")
            elif kind == "voiceurl" and data:
                out.append({"type": "record", "data": {"file": self._mapping_ref(data)}})
            elif kind == "video" and data:
                out.append({"type": "video", "data": {"file": self._file_ref(data)}})
            elif kind == "videourl" and data:
                out.append({"type": "video", "data": {"file": self._mapping_ref(data)}})
            elif kind == "music" and data:
                out.append({"type": "music", "data": {"type": "163", "id": str(data)}})
            elif kind == "file" and data:
                # Files are sent via upload_group_file/upload_private_file in _send_normal.
                logger.debug("SnowLuma outbound segment deferred kind=file reason=upload_action")
            elif kind == "forward" and not in_forward:
                # Forward nodes are sent via send_*_forward_msg in _send_normal.
                logger.debug("SnowLuma outbound segment deferred kind=forward reason=forward_action")
            elif kind not in {"", "text"}:
                logger.warning("SnowLuma outbound segment ignored kind={} reason=unsupported", kind)
        return out



    def _extract_forward_nodes(self, root: Seg) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        items = root.data if root.type == "seglist" and isinstance(root.data, list) else [root]
        for item in items:
            if isinstance(item, dict):
                try:
                    item = Seg.from_dict(item)
                except Exception:
                    continue
            if not isinstance(item, Seg):
                continue
            if item.type == "seglist":
                nodes.extend(self._extract_forward_nodes(item))
                continue
            if item.type != "forward" or not isinstance(item.data, list):
                continue
            for raw_node in item.data:
                try:
                    mb = MessageBase.from_dict(raw_node) if isinstance(raw_node, dict) else raw_node
                    content = self._outbound_segments(mb.message_segment, in_forward=True)
                    ui = mb.message_info.user_info
                    if ui is None or not content:
                        continue
                    nodes.append({
                        "type": "node",
                        "data": {
                            "name": ui.user_cardname or ui.user_nickname or "QQ用户",
                            "uin": ui.user_id,
                            "user_id": ui.user_id,
                            "content": content,
                        },
                    })
                except Exception as exc:
                    logger.warning("SnowLuma outbound forward node skipped reason=invalid_node error={}", safe_exception(exc))
        return nodes

    def _extract_file_uploads(self, root: Seg) -> list[dict[str, Any]]:
        uploads: list[dict[str, Any]] = []
        items = root.data if root.type == "seglist" and isinstance(root.data, list) else [root]
        for item in items:
            if isinstance(item, dict):
                try:
                    item = Seg.from_dict(item)
                except Exception:
                    continue
            if not isinstance(item, Seg):
                continue
            if item.type == "seglist":
                uploads.extend(self._extract_file_uploads(item))
                continue
            if item.type != "file" or not item.data:
                continue
            data = item.data
            if isinstance(data, Mapping):
                ref = str(data.get("file") or data.get("path") or data.get("url") or "").strip()
                name = str(data.get("name") or data.get("filename") or data.get("file_name") or "").strip()
                folder = str(data.get("folder") or "").strip()
                folder_id = str(data.get("folder_id") or "").strip()
            else:
                ref = str(data).strip()
                name = ""
                folder = ""
                folder_id = ""
            if not ref:
                continue
            if not ref.startswith(("base64://", "file://", "http://", "https://")):
                ref = "file://" + ref
            if not name:
                clean = ref.removeprefix("file://")
                name = Path(clean).name if not clean.startswith(("http://", "https://", "base64://")) else "upload.bin"
            payload: dict[str, Any] = {"file": ref, "name": name}
            if folder:
                payload["folder"] = folder
            if folder_id:
                payload["folder_id"] = folder_id
            uploads.append(payload)
        return uploads

    async def _echo_message_id(self, message_base: MessageBase, actual_id: str) -> None:
        logger.debug(
            "SnowLuma message_id_echo handoff start source_message_id={} actual_message_id={}",
            message_base.message_info.message_id or "-",
            actual_id,
        )
        try:
            await self.router.send_custom_message(
                platform=message_base.message_info.platform,
                message_type_name="message_id_echo",
                message={
                    "type": "echo",
                    "echo": message_base.message_info.message_id,
                    "actual_id": actual_id,
                },
            )
            logger.info("SnowLuma message_id_echo handoff success actual_message_id={}", actual_id)
        except Exception as exc:
            logger.warning("SnowLuma message_id_echo handoff failed actual_message_id={} error={}", actual_id, safe_exception(exc))

    # ------------------------------------------------------------------
    # OneBot helpers
    # ------------------------------------------------------------------
    async def _get_group_name(self, group_id: int) -> str:
        if group_id in self._group_name_cache:
            logger.debug("SnowLuma group lookup cache hit group_id={}", group_id)
            return self._group_name_cache[group_id]
        try:
            logger.debug("SnowLuma group lookup start group_id={}", group_id)
            response = await self.client.call_action("get_group_info", {"group_id": group_id, "no_cache": False})
            data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
            name = str(data.get("group_name") or group_id)
            self._group_name_cache[group_id] = name
            logger.debug("SnowLuma group lookup success group_id={}", group_id)
            return name
        except Exception as exc:
            logger.warning("SnowLuma group lookup failed group_id={} error={}", group_id, safe_exception(exc))
            return str(group_id)

    async def _get_member_info(self, group_id: int | None, user_id: int) -> dict[str, Any]:
        if not group_id:
            return {}
        key = (group_id, user_id)
        if key in self._member_cache:
            logger.debug("SnowLuma member lookup cache hit group_id={} user_id={}", group_id, user_id)
            return self._member_cache[key]
        try:
            logger.debug("SnowLuma member lookup start group_id={} user_id={}", group_id, user_id)
            response = await self.client.call_action(
                "get_group_member_info",
                {"group_id": group_id, "user_id": user_id, "no_cache": True},
            )
            data = dict(response.get("data") or {}) if isinstance(response.get("data"), Mapping) else {}
            self._member_cache[key] = data
            logger.debug("SnowLuma member lookup success group_id={} user_id={} found={}", group_id, user_id, bool(data))
            return data
        except Exception as exc:
            logger.warning("SnowLuma member lookup failed group_id={} user_id={} error={}", group_id, user_id, safe_exception(exc))
            return {}

    async def _get_self_info(self) -> dict[str, Any]:
        try:
            logger.debug("SnowLuma self lookup start")
            response = await self.client.call_action("get_login_info", {})
            data = dict(response.get("data") or {}) if isinstance(response.get("data"), Mapping) else {}
            logger.debug("SnowLuma self lookup success found={}", bool(data))
            return data
        except Exception as exc:
            logger.warning("SnowLuma self lookup failed error={}", safe_exception(exc))
            return {}

    async def _get_stranger_info(self, user_id: int) -> dict[str, Any]:
        try:
            logger.debug("SnowLuma stranger lookup start user_id={}", user_id)
            response = await self.client.call_action("get_stranger_info", {"user_id": user_id})
            data = dict(response.get("data") or {}) if isinstance(response.get("data"), Mapping) else {}
            logger.debug("SnowLuma stranger lookup success user_id={} found={}", user_id, bool(data))
            return data
        except Exception as exc:
            logger.warning("SnowLuma stranger lookup failed user_id={} error={}", user_id, safe_exception(exc))
            return {}

    async def _media_base64(self, data: Mapping[str, Any], action: str) -> str:
        logger.debug("SnowLuma media lookup start action={} keys={}", action, ",".join(sorted(str(key) for key in data)) or "-")
        for key in ("base64", "data"):
            value = data.get(key)
            if isinstance(value, str) and value:
                if value.startswith("base64://"):
                    logger.debug("SnowLuma media lookup success action={} source={} size={}", action, key, len(value.removeprefix("base64://")))
                    return value.removeprefix("base64://")
                try:
                    base64.b64decode(value, validate=True)
                    logger.debug("SnowLuma media lookup success action={} source={} size={}", action, key, len(value))
                    return value
                except Exception:
                    pass
        for key in ("url", "path", "file_path", "file"):
            value = str(data.get(key) or "").strip()
            b = await self._load_binary(value)
            if b:
                logger.debug("SnowLuma media lookup success action={} source={} size={}", action, key, len(b))
                return base64.b64encode(b).decode()
        file_name = str(data.get("file") or "").strip()
        if file_name:
            try:
                response = await self.client.call_action(action, {"file": file_name})
                action_data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
                for key in ("base64", "url", "path", "file"):
                    value = str(action_data.get(key) or "").strip()
                    if key == "base64" and value:
                        logger.debug("SnowLuma media lookup success action={} source=action_response size={}", action, len(value.removeprefix("base64://")))
                        return value.removeprefix("base64://")
                    b = await self._load_binary(value)
                    if b:
                        logger.debug("SnowLuma media lookup success action={} source=action_response size={}", action, len(b))
                        return base64.b64encode(b).decode()
            except Exception as exc:
                logger.warning("SnowLuma media lookup failed action={} error={}", action, safe_exception(exc))
        logger.warning("SnowLuma media lookup failed action={} reason=not_found", action)
        return ""

    async def _record_base64(self, data: Mapping[str, Any]) -> str:
        file_name = str(data.get("file") or data.get("file_id") or "").strip()
        if file_name:
            try:
                response = await self.client.call_action("get_record", {"file": file_name, "out_format": "wav"})
                action_data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
                b64 = str(action_data.get("base64") or "").strip()
                if b64:
                    logger.debug("SnowLuma media lookup success action=get_record source=base64 size={}", len(b64.removeprefix("base64://")))
                    return b64.removeprefix("base64://")
                for key in ("url", "path", "file"):
                    binary = await self._load_binary(str(action_data.get(key) or ""))
                    if binary:
                        logger.debug("SnowLuma media lookup success action=get_record source={} size={}", key, len(binary))
                        return base64.b64encode(binary).decode()
            except Exception as exc:
                logger.warning("SnowLuma media lookup failed action=get_record error={}", safe_exception(exc))
        return await self._media_base64(data, "get_record")

    async def _load_binary(self, ref: str) -> bytes:
        ref = ref.strip()
        if not ref:
            return b""
        if ref.startswith("base64://"):
            try:
                binary = base64.b64decode(ref.removeprefix("base64://"))
                logger.debug("SnowLuma binary load success source=base64 size={}", len(binary))
                return binary
            except Exception as exc:
                logger.warning("SnowLuma binary load failed source=base64 error={}", safe_exception(exc))
                return b""
        if ref.startswith(("http://", "https://")):
            if self._http is None or self._http.closed:
                self._http = ClientSession(timeout=ClientTimeout(total=20))
            try:
                async with self._http.get(ref) as response:
                    if response.status >= 400:
                        logger.warning("SnowLuma binary load failed source=url status={}", response.status)
                        return b""
                    binary = await response.read()
                    logger.debug("SnowLuma binary load success source=url endpoint={} size={}", safe_endpoint(ref), len(binary))
                    return binary
            except Exception as exc:
                logger.warning("SnowLuma binary load failed source=url endpoint={} error={}", safe_endpoint(ref), safe_exception(exc))
                return b""
        if ref.startswith("file://"):
            ref = ref.removeprefix("file://")
        try:
            path = Path(ref)
            if path.is_file():
                binary = await asyncio.to_thread(path.read_bytes)
                logger.debug("SnowLuma binary load success source=file size={}", len(binary))
                return binary
        except Exception as exc:
            logger.warning("SnowLuma binary load failed source=file error={}", safe_exception(exc))
            pass
        logger.warning("SnowLuma binary load failed source=ref reason=not_found")
        return b""

    @staticmethod
    def _file_ref(data: Any) -> str:
        if isinstance(data, Mapping):
            b64 = str(data.get("binary_data_base64") or "").strip()
            if b64:
                return "base64://" + b64
            ref = str(data.get("file") or data.get("path") or data.get("url") or "").strip()
            return SnowLumaBridge._normalize_ref(ref)
        value = str(data or "").strip()
        if not value:
            return ""
        if value.startswith(("base64://", "file://", "http://", "https://")):
            return value
        # Core's image/emoji/voice segment data is normally raw base64.
        try:
            base64.b64decode(value, validate=True)
            return "base64://" + value
        except Exception:
            return SnowLumaBridge._normalize_ref(value)

    @staticmethod
    def _mapping_ref(data: Any) -> str:
        if isinstance(data, Mapping):
            return SnowLumaBridge._normalize_ref(str(data.get("file") or data.get("url") or data.get("path") or ""))
        return SnowLumaBridge._normalize_ref(str(data or ""))

    @staticmethod
    def _normalize_ref(value: str) -> str:
        if not value:
            return ""
        if value.startswith(("base64://", "file://", "http://", "https://")):
            return value
        return "file://" + value

    @staticmethod
    def _file_size(value: Any) -> int:
        if isinstance(value, bytes):
            return len(value)
        if isinstance(value, str):
            if value.startswith("base64://"):
                return len(value.removeprefix("base64://"))
            return len(value)
        if isinstance(value, Mapping):
            return sum(len(item) for item in value.values() if isinstance(item, (str, bytes)))
        return 0

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            result = int(str(value).strip())
            return result if result != 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _contains_visual(segments: list[Seg]) -> bool:
        for seg in segments:
            if seg.type in VISUAL_TYPES:
                return True
            if seg.type == "seglist" and isinstance(seg.data, list):
                nested = [x for x in seg.data if isinstance(x, Seg)]
                if SnowLumaBridge._contains_visual(nested):
                    return True
        return False
