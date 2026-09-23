import time
import json
import asyncio
import random
import websockets as Server
from typing import Tuple, Optional

from src.logger import logger
from src.config import global_config
from src.database import BanUser, db_manager, is_identical
from . import NoticeType, get_accept_format
from .message_sending import message_send_instance
from src.send_handler.nc_sending import nc_message_sender
from .message_handler import message_handler
from ncnk_message import (
    FormatInfo,
    UserInfo,
    GroupInfo,
    Seg,
    BaseMessageInfo,
    MessageBase,
    build_system_event,
    build_system_event_route,
)

from src.utils import (
    get_group_info,
    get_member_info,
    get_self_info,
    get_stranger_info,
    read_ban_list,
)

notice_queue: asyncio.Queue[MessageBase] = asyncio.Queue(maxsize=100)
unsuccessful_notice_queue: asyncio.Queue[MessageBase] = asyncio.Queue(maxsize=3)


def _format_duration(duration_seconds: int) -> str:
    """将秒数格式化为友好的时长描述"""
    if duration_seconds < 60:
        return f"{duration_seconds}秒"
    minutes = duration_seconds // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if hours < 24:
        if remaining_minutes > 0:
            return f"{hours}小时{remaining_minutes}分钟"
        return f"{hours}小时"
    days = hours // 24
    remaining_hours = hours % 24
    if remaining_hours > 0:
        return f"{days}天{remaining_hours}小时"
    return f"{days}天"


class NoticeHandler:
    banned_list: list[BanUser] = []  # 当前仍在禁言中的用户列表
    lifted_list: list[BanUser] = []  # 已经自然解除禁言
    self_muted_groups: dict[int, float] = {}  # Bot自身被禁言的群 {group_id: mute_end_timestamp}

    def __init__(self):
        self.server_connection: Server.ServerConnection = None

    async def set_server_connection(self, server_connection: Server.ServerConnection) -> None:
        """设置Napcat连接"""
        self.server_connection = server_connection

        while self.server_connection.state != Server.State.OPEN:
            await asyncio.sleep(0.5)
        self.banned_list, self.lifted_list = await read_ban_list(self.server_connection)

        asyncio.create_task(self.auto_lift_detect())
        asyncio.create_task(self.send_notice())
        asyncio.create_task(self.handle_natural_lift())

    def _ban_operation(self, group_id: int, user_id: Optional[int] = None, lift_time: Optional[int] = None) -> None:
        """
        将用户禁言记录添加到self.banned_list中
        如果是全体禁言，则user_id为0
        """
        if user_id is None:
            user_id = 0  # 使用0表示全体禁言
            lift_time = -1
        ban_record = BanUser(user_id=user_id, group_id=group_id, lift_time=lift_time)
        for record in self.banned_list:
            if is_identical(record, ban_record):
                self.banned_list.remove(record)
                self.banned_list.append(ban_record)
                db_manager.create_ban_record(ban_record)  # 作为更新
                return
        self.banned_list.append(ban_record)
        db_manager.create_ban_record(ban_record)  # 添加到数据库

    def _lift_operation(self, group_id: int, user_id: Optional[int] = None) -> None:
        """
        从self.lifted_group_list中移除已经解除全体禁言的群
        """
        if user_id is None:
            user_id = 0  # 使用0表示全体禁言
        ban_record = BanUser(user_id=user_id, group_id=group_id, lift_time=-1)
        self.lifted_list.append(ban_record)
        db_manager.delete_ban_record(ban_record)  # 删除数据库中的记录

    async def _handle_generic_group_notice(
        self,
        raw_message: dict,
        group_id: int,
    ) -> tuple[Seg | None, dict | None]:
        """将未被专用分支覆盖的低频群通知转换为 structured system_event。"""
        if not group_id:
            return None, None

        notice_type = str(raw_message.get("notice_type") or "unknown")
        sub_type = str(raw_message.get("sub_type") or "").strip()

        # 这些属于传输/输入状态，不进入 Core。
        if notice_type in {"input_status", "heartbeat", "meta_event"}:
            return None, None

        async def resolve_member_name(raw_id: object) -> str | None:
            if not isinstance(raw_id, int) or raw_id == 0:
                return None
            try:
                info = await get_member_info(self.server_connection, group_id, raw_id)
            except Exception:
                info = None
            if info:
                return str(info.get("card") or info.get("nickname") or raw_id)
            return str(raw_id)

        user_id = raw_message.get("user_id")
        operator_id = raw_message.get("operator_id")
        target_id = raw_message.get("target_id")

        target_raw_id = user_id if isinstance(user_id, int) else target_id
        actor_raw_id = operator_id if isinstance(operator_id, int) else None
        target_name = await resolve_member_name(target_raw_id)
        actor_name = await resolve_member_name(actor_raw_id)

        actor = (
            {"user_id": str(actor_raw_id), "name": actor_name or str(actor_raw_id)}
            if isinstance(actor_raw_id, int) and actor_raw_id != 0
            else None
        )
        target = (
            {"user_id": str(target_raw_id), "name": target_name or str(target_raw_id)}
            if isinstance(target_raw_id, int) and target_raw_id != 0
            else None
        )

        display_target = target_name or "群成员"
        event_type = f"qq.{notice_type}"
        if sub_type:
            event_type = f"{event_type}.{sub_type}"

        if notice_type == "group_admin":
            if sub_type == "set":
                text = f"{display_target}被设为群管理员"
            elif sub_type == "unset":
                text = f"{display_target}被取消群管理员"
            else:
                text = f"{display_target}的群管理员状态发生变化"
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
            card_new = str(raw_message.get("card_new") or raw_message.get("new_card") or "").strip()
            card_old = str(raw_message.get("card_old") or raw_message.get("old_card") or "").strip()
            if card_new:
                text = f"{display_target}的群名片变更为{card_new}"
            elif card_old:
                text = f"{display_target}的群名片发生了变化"
            else:
                text = f"{display_target}的群名片发生了变化"
        elif notice_type == "notify" and sub_type == "honor":
            honor_type = str(raw_message.get("honor_type") or raw_message.get("title") or "群荣誉").strip()
            text = f"{display_target}获得了{honor_type}"
        elif "title" in notice_type or sub_type in {"title", "special_title"}:
            title = str(raw_message.get("title") or raw_message.get("special_title") or "").strip()
            text = f"{display_target}的群头衔变更为{title}" if title else f"{display_target}的群头衔发生了变化"
        else:
            type_label = notice_type if not sub_type else f"{notice_type}.{sub_type}"
            text = f"群内发生了{type_label}通知"
            if target_name:
                text = f"{target_name}触发了{type_label}通知"

        event_meta = {
            "type": event_type,
            "actor": actor,
            "target": target,
            "data": {
                "group_id": str(group_id),
                "notice_type": notice_type,
                "sub_type": sub_type or None,
                "raw_event": raw_message,
            },
        }
        return Seg(type="text", data=text), event_meta

    @staticmethod
    def _notice_requires_chat_admission(
        notice_type: str | None,
        sub_type: str | None,
        group_id: int,
        user_id: int | None,
    ) -> bool:
        """Return whether this notice can become a forwarded system event."""
        if notice_type == NoticeType.friend_recall:
            return False

        if notice_type == NoticeType.notify and sub_type == NoticeType.Notify.poke:
            return bool(global_config.chat.enable_poke and user_id not in (None, 0))

        if not group_id or notice_type in {"input_status", "heartbeat", "meta_event"}:
            return False

        # Keep the existing malformed group-ban behavior: without a target ID,
        # its dedicated handler drops the notice before it can be forwarded.
        if notice_type == NoticeType.group_ban and user_id is None:
            return False

        return True

    async def _check_notice_chat_admission(
        self,
        raw_message: dict,
        group_id: int,
        user_id: int | None,
        *,
        admission_user_id: int | None = None,
        ignore_bot: bool = False,
        ignore_global_list: bool = False,
        ignore_self_muted: bool = False,
    ) -> bool:
        """Apply the ordinary chat admission policy before notice enrichment."""
        if group_id:
            # Prefer the event operator as the user whose activity caused a
            # generic notice. Some notice types only expose user_id, which is
            # then the best available actor/subject identity.
            if admission_user_id is None:
                raw_operator_id = raw_message.get("operator_id")
                admission_user_id = (
                    raw_operator_id
                    if isinstance(raw_operator_id, int) and raw_operator_id != 0
                    else user_id
                )

            has_user = isinstance(admission_user_id, int) and admission_user_id != 0
            checked_user_id = admission_user_id if has_user else 0
            allowed = await message_handler.check_allow_to_chat(
                checked_user_id,
                group_id,
                ignore_bot or not has_user,
                ignore_global_list or not has_user,
                ignore_self_muted=ignore_self_muted,
            )
        else:
            if not isinstance(user_id, int) or user_id == 0:
                logger.warning("私聊notice缺少有效 user_id，系统事件因聊天准入被丢弃")
                return False
            allowed = await message_handler.check_allow_to_chat(
                user_id,
                None,
                ignore_bot,
                ignore_global_list,
            )

        if not allowed:
            logger.warning(
                "notice/system event 因聊天准入策略被丢弃: "
                f"type={raw_message.get('notice_type')}, group_id={group_id or None}, "
                f"user_id={admission_user_id if group_id else user_id}"
            )
        return allowed

    async def handle_notice(self, raw_message: dict) -> None:
        notice_type = raw_message.get("notice_type")
        sub_type = raw_message.get("sub_type")
        message_time: float = time.time()

        raw_group_id = raw_message.get("group_id")
        raw_user_id = raw_message.get("user_id")
        target_id = raw_message.get("target_id")

        group_id = raw_group_id if isinstance(raw_group_id, int) else 0
        user_id = raw_user_id if isinstance(raw_user_id, int) else None
        raw_group_ban_operator_id = raw_message.get("operator_id")
        group_ban_operator_id = (
            raw_group_ban_operator_id
            if isinstance(raw_group_ban_operator_id, int) and raw_group_ban_operator_id != 0
            else None
        )

        own_lift_while_muted = (
            notice_type == NoticeType.group_ban
            and sub_type == NoticeType.GroupBan.lift_ban
            and group_id in self.self_muted_groups
            and isinstance(raw_message.get("self_id"), int)
            and raw_message.get("self_id") != 0
            and user_id == raw_message.get("self_id")
        )

        if self._notice_requires_chat_admission(notice_type, sub_type, group_id, user_id):
            if not await self._check_notice_chat_admission(
                raw_message,
                group_id,
                user_id,
                # group_ban's operator caused the event; use its target only
                # when the adapter omitted an operator identity.
                admission_user_id=(
                    (group_ban_operator_id if group_ban_operator_id is not None else user_id)
                    if notice_type == NoticeType.group_ban else None
                ),
                # 群禁言事件的 operator 仍是准入策略中的真实行为主体。
                # 自解除禁言只绕过当前 mute fence，不能绕过 QQ Bot 策略。
                ignore_bot=False,
                ignore_self_muted=own_lift_while_muted,
            ):
                return None

        handled_message: Seg | None = None
        event_meta: dict | None = None
        system_notice: bool = False

        match notice_type:
            case NoticeType.friend_recall:
                logger.info("好友撤回一条消息")
                logger.info(f"撤回消息ID：{raw_message.get('message_id')}, 撤回时间：{raw_message.get('time')}")
                logger.warning("暂时不支持撤回消息处理")
            case NoticeType.group_recall:
                logger.info("群内用户撤回一条消息")
                logger.info(f"撤回消息ID：{raw_message.get('message_id')}, 撤回时间：{raw_message.get('time')}")
                handled_message, event_meta = await self._handle_generic_group_notice(raw_message, group_id)
            case NoticeType.notify:
                sub_type = raw_message.get("sub_type")
                match sub_type:
                    case NoticeType.Notify.poke:
                        if user_id in (None, 0):
                            logger.warning("戳一戳事件缺少有效 user_id，忽略")
                            return None
                        if global_config.chat.enable_poke:
                            logger.info("处理戳一戳消息")
                            handled_message, event_meta = await self.handle_poke_notify(raw_message, group_id, user_id)
                        else:
                            logger.warning("戳一戳消息被禁用，取消戳一戳处理")
                    case _:
                        logger.info(f"使用通用群通知处理: {notice_type}.{sub_type}")
                        handled_message, event_meta = await self._handle_generic_group_notice(raw_message, group_id)
            case NoticeType.group_ban:
                if not group_id:
                    logger.warning("群禁言事件缺少有效 group_id，忽略")
                    return None
                if user_id is None:
                    logger.warning("群禁言事件缺少有效 user_id，忽略")
                    return None

                sub_type = raw_message.get("sub_type")
                match sub_type:
                    case NoticeType.GroupBan.ban:
                        logger.info("处理群禁言")
                        handled_message, event_meta = await self.handle_ban_notify(raw_message, group_id)
                        system_notice = True
                    case NoticeType.GroupBan.lift_ban:
                        logger.info("处理解除群禁言")
                        handled_message, event_meta = await self.handle_lift_ban_notify(raw_message, group_id)
                        system_notice = True
                    case _:
                        logger.info(f"使用通用群通知处理: {notice_type}.{sub_type}")
                        handled_message, event_meta = await self._handle_generic_group_notice(raw_message, group_id)
            case _:
                if group_id:
                    logger.info(f"使用通用群通知处理: {notice_type}")
                    handled_message, event_meta = await self._handle_generic_group_notice(raw_message, group_id)
                else:
                    logger.warning(f"不支持的notice类型: {notice_type}")
                    return None

        if not handled_message or not event_meta:
            logger.warning("notice处理失败或不支持")
            return None

        group_info: GroupInfo | None = None
        if group_id:
            fetched_group_info = await get_group_info(self.server_connection, group_id)
            group_name: str | None = None
            if fetched_group_info:
                group_name = fetched_group_info.get("group_name")
            else:
                logger.warning("无法获取notice消息所在群的名称")
            group_info = GroupInfo(
                platform=global_config.nachobot_server.platform_name,
                group_id=group_id,
                group_name=group_name,
            )

        try:
            system_event = build_system_event(
                event_meta.get("type"),
                actor=event_meta.get("actor"),
                target=event_meta.get("target"),
                data=event_meta.get("data") or {},
            )
        except ValueError:
            logger.warning("notice事件元数据不符合 structured system_event 合约，忽略")
            return None

        additional_config = {
            "target_id": target_id,
            "system_event": system_event,
        }
        if not group_info and event_meta.get("system_event_route") is not None:
            additional_config["system_event_route"] = event_meta["system_event_route"]

        message_info = BaseMessageInfo(
            platform=global_config.nachobot_server.platform_name,
            message_id="notice",
            time=message_time,
            # 系统事件没有 message sender。真实操作者仅存在于 system_event.actor。
            user_info=None,
            group_info=group_info,
            template_info=None,
            format_info=FormatInfo(
                content_format=["text", "notify"],
                accept_format=get_accept_format(bool(global_config.voice.use_tts)),
            ),
            additional_config=additional_config,
        )

        try:
            serialized_raw_message = json.dumps(raw_message)
            message_base = MessageBase(
                message_info=message_info,
                message_segment=handled_message,
                raw_message=serialized_raw_message,
            )
        except (TypeError, ValueError):
            logger.warning("notice原始事件无法序列化，忽略")
            return None

        # 自解除禁言只为准入检查临时绕过 mute fence。必须等 structured
        # event 及完整消息信封均构建成功后，才真正恢复该群链路。
        if own_lift_while_muted and group_id in self.self_muted_groups:
            del self.self_muted_groups[group_id]
            logger.info(f"检测到Bot在群 {group_id} 的禁言被提前解除，恢复链路")

        logger.info(
            f"发送系统事件: type={system_event['type']}, "
            f"actor={system_event['actor']}, target={system_event['target']}"
        )

        # 保留旧版戳一戳的独立快速回戳分支。它只负责发送平台动作，不能
        # 取代/阻断上面的 structured event Core 转发。
        fast_poke_eligible = (
            system_event.get("type") == "qq.poke"
            and isinstance(raw_message.get("self_id"), int)
            and raw_message.get("target_id") == raw_message.get("self_id")
        )
        fast_poke_result: dict | None = None
        if fast_poke_eligible and random.random() < 0.5:
            fast_poke_result = {"triggered": True, "result": "pending"}
            logger.info(
                f"快速回戳触发: group_id={group_id or None}, user_id={user_id}, "
                f"message_id={message_base.message_info.message_id}"
            )
            try:
                poke_params = {"user_id": user_id}
                if group_id:
                    poke_params["group_id"] = group_id
                poke_result = await asyncio.wait_for(
                    nc_message_sender.send_message_to_napcat("send_poke", poke_params),
                    timeout=1.0,
                )
                if isinstance(poke_result, dict) and poke_result.get("status") == "ok":
                    fast_poke_result["result"] = "success"
                    logger.info("快速回戳成功")
                else:
                    fast_poke_result["result"] = "failed"
                    logger.warning(f"快速回戳失败，Napcat返回：{poke_result}")
            except asyncio.TimeoutError:
                fast_poke_result["result"] = "timeout"
                logger.warning("快速回戳超时，继续转发 structured event")
            except Exception as exc:
                fast_poke_result["result"] = "error"
                logger.warning(f"快速回戳异常，继续转发 structured event: {exc}")
        else:
            reason = "不满足目标条件" if not fast_poke_eligible else "50%概率跳过"
            logger.info(f"快速回戳跳过: {reason}")

        # 将适配器侧快捷动作的结果随同环境事件送回 Core。这样 Core 的
        # `[所见]` 日志和事件上下文都能明确知道是否实际触发过回戳，且
        # 不会把该动作伪装成普通用户消息。
        if fast_poke_result is not None:
            system_event_data = system_event.setdefault("data", {})
            if isinstance(system_event_data, dict):
                system_event_data["fast_poke"] = fast_poke_result

        if system_notice:
            await self.put_notice(message_base)
            logger.info("structured event 转发入队完成（未等待 Core 处理确认）")
        else:
            logger.info("发送到Nachobot处理通知信息")
            try:
                forward_status = await message_send_instance.message_send(message_base)
            except Exception as exc:
                logger.warning(f"structured event 转发 await 失败: {exc}")
            else:
                if forward_status:
                    logger.info("structured event 转发 await 完成")
                else:
                    logger.warning("structured event 转发 await 完成，但未获得成功确认")

    async def handle_poke_notify(
        self, raw_message: dict, group_id: int, user_id: int
    ) -> Tuple[Seg | None, dict | None]:
        # sourcery skip: merge-comparisons, merge-duplicate-blocks, remove-redundant-if, remove-unnecessary-else, swap-if-else-branches
        self_info = await get_self_info(self.server_connection)

        if not self_info:
            logger.error("自身信息获取失败")
            return None, None

        self_id = raw_message.get("self_id")
        raw_target_id = raw_message.get("target_id")
        if not isinstance(raw_target_id, int):
            logger.warning("戳一戳事件缺少有效 target_id，忽略")
            return None, None
        target_id = raw_target_id

        target_name: str | None = None
        raw_info = raw_message.get("raw_info") or []

        if group_id:
            user_qq_info = await get_member_info(self.server_connection, group_id, user_id)
        else:
            user_qq_info = await get_stranger_info(self.server_connection, user_id)

        if user_qq_info:
            user_name = user_qq_info.get("nickname")
            user_cardname = user_qq_info.get("card")
        else:
            user_name = "QQ用户"
            user_cardname = None
            logger.info("无法获取戳一戳对方的用户昵称")

        actor_name = user_cardname or user_name or "QQ用户"

        if self_id == target_id:
            display_name = ""
            target_name = self_info.get("nickname") or "NachoBot"
        elif self_id == user_id:
            # Bot 自己发起的戳一戳不作为外部系统事件送入 Core。
            return None, None
        else:
            if group_id:
                fetched_member_info = await get_member_info(self.server_connection, group_id, target_id)
                if fetched_member_info:
                    target_name = fetched_member_info.get("card") or fetched_member_info.get("nickname") or "QQ用户"
                else:
                    target_name = "QQ用户"
                    logger.info("无法获取被戳一戳方的用户昵称")
                display_name = actor_name
            else:
                return None, None

        first_txt: str = "戳了戳"
        second_txt: str = ""
        try:
            first_txt = raw_info[2].get("txt", "戳了戳")
            second_txt = raw_info[4].get("txt", "")
        except Exception as e:
            logger.warning(f"解析戳一戳消息失败: {str(e)}，将使用默认文本")

        seg_data = Seg(
            type="text",
            data=f"{display_name}{first_txt}{target_name}{second_txt}（这是QQ的一个功能，用于提及某人，但没那么明显）",
        )
        event_meta = {
            "type": "qq.poke",
            "actor": {
                "user_id": str(user_id),
                "name": actor_name,
            },
            "target": {
                "user_id": str(target_id),
                "name": target_name or "QQ用户",
            },
            "data": {
                "group_id": str(group_id) if group_id else None,
                "raw_info": raw_info,
            },
        }
        if not group_id:
            # Private events are senderless; this explicit peer identity is
            # used only to select the private ChatStream.
            event_meta["system_event_route"] = build_system_event_route(
                global_config.nachobot_server.platform_name,
                user_id=str(user_id),
                nickname=user_name,
                cardname=user_cardname,
            )
        return seg_data, event_meta

    async def handle_ban_notify(self, raw_message: dict, group_id: int) -> Tuple[Seg, dict] | Tuple[None, None]:
        if not group_id:
            logger.error("群ID不能为空，无法处理禁言通知")
            return None, None

        raw_operator_id = raw_message.get("operator_id")
        if not isinstance(raw_operator_id, int):
            logger.warning("群禁言事件缺少有效 operator_id，忽略")
            return None, None
        operator_id = raw_operator_id

        member_info = await get_member_info(self.server_connection, group_id, operator_id)
        if member_info:
            operator_nickname = member_info.get("nickname")
            operator_cardname = member_info.get("card")
        else:
            logger.warning("无法获取禁言执行者的昵称")
            operator_nickname = "QQ用户"
            operator_cardname = None

        operator_display = operator_cardname or operator_nickname or "QQ用户"

        raw_user_id = raw_message.get("user_id")
        if not isinstance(raw_user_id, int):
            logger.warning("群禁言事件缺少有效 user_id，忽略")
            return None, None
        user_id = raw_user_id

        raw_duration = raw_message.get("duration")
        if not isinstance(raw_duration, (int, float)):
            logger.error("禁言时长无效，无法处理禁言通知")
            return None, None
        duration = int(raw_duration)

        target_meta: dict | None
        if user_id == 0:
            self._ban_operation(group_id)
            natural_text = f"{operator_display}开启了全体禁言"
            target_meta = None
        else:
            fetched_member_info = await get_member_info(self.server_connection, group_id, user_id)
            if fetched_member_info:
                user_nickname = fetched_member_info.get("nickname") or "QQ用户"
                user_cardname = fetched_member_info.get("card")
            else:
                user_nickname = "QQ用户"
                user_cardname = None

            self._ban_operation(group_id, user_id, int(time.time() + duration))

            self_info = await get_self_info(self.server_connection)
            if self_info and str(user_id) == str(self_info.get("user_id")):
                mute_end_time = time.time() + duration
                self.self_muted_groups[group_id] = mute_end_time
                logger.warning(
                    f"Bot自身在群 {group_id} 被禁言 {_format_duration(duration)}，"
                    f"将自动切断该群到核心的链路直到 {time.strftime('%H:%M:%S', time.localtime(mute_end_time))}"
                )

            user_display = user_cardname or user_nickname or "QQ用户"
            natural_text = f"{operator_display}将{user_display}禁言了{_format_duration(duration)}"
            target_meta = {
                "user_id": str(user_id),
                "name": user_display,
            }

        seg_data = Seg(type="text", data=natural_text)
        event_meta = {
            "type": "qq.group_ban",
            "actor": {
                "user_id": str(operator_id),
                "name": operator_display,
            },
            "target": target_meta,
            "data": {
                "duration": duration,
                "group_id": str(group_id),
                "all_members": user_id == 0,
            },
        }
        return seg_data, event_meta

    async def handle_lift_ban_notify(
        self, raw_message: dict, group_id: int
    ) -> Tuple[Seg, dict] | Tuple[None, None]:
        if not group_id:
            logger.error("群ID不能为空，无法处理解除禁言通知")
            return None, None

        raw_operator_id = raw_message.get("operator_id")
        if not isinstance(raw_operator_id, int):
            logger.warning("解除禁言事件缺少有效 operator_id，忽略")
            return None, None
        operator_id = raw_operator_id

        member_info = await get_member_info(self.server_connection, group_id, operator_id)
        if member_info:
            operator_nickname = member_info.get("nickname")
            operator_cardname = member_info.get("card")
        else:
            logger.warning("无法获取解除禁言执行者的昵称")
            operator_nickname = "QQ用户"
            operator_cardname = None

        operator_display = operator_cardname or operator_nickname or "QQ用户"

        raw_user_id = raw_message.get("user_id")
        if not isinstance(raw_user_id, int):
            logger.warning("解除禁言事件缺少有效 user_id，忽略")
            return None, None
        user_id = raw_user_id

        target_meta: dict | None
        if user_id == 0:
            self._lift_operation(group_id)
            natural_text = f"{operator_display}关闭了全体禁言"
            target_meta = None
        else:
            fetched_member_info = await get_member_info(self.server_connection, group_id, user_id)
            if fetched_member_info:
                user_nickname = fetched_member_info.get("nickname") or "QQ用户"
                user_cardname = fetched_member_info.get("card")
            else:
                logger.warning("无法获取被解除禁言用户的昵称")
                user_nickname = "QQ用户"
                user_cardname = None

            self._lift_operation(group_id, user_id)

            user_display = user_cardname or user_nickname or "QQ用户"
            natural_text = f"{operator_display}解除了{user_display}的禁言"
            target_meta = {
                "user_id": str(user_id),
                "name": user_display,
            }

        seg_data = Seg(type="text", data=natural_text)
        event_meta = {
            "type": "qq.group_lift_ban",
            "actor": {
                "user_id": str(operator_id),
                "name": operator_display,
            },
            "target": target_meta,
            "data": {
                "group_id": str(group_id),
                "all_members": user_id == 0,
            },
        }
        return seg_data, event_meta

    async def put_notice(self, message_base: MessageBase) -> None:
        """
        将处理后的通知消息放入通知队列
        """
        if notice_queue.full() or unsuccessful_notice_queue.full():
            logger.warning("通知队列已满，可能是多次发送失败，消息丢弃")
        else:
            await notice_queue.put(message_base)

    async def handle_natural_lift(self) -> None:
        while True:
            if len(self.lifted_list) != 0:
                lift_record = self.lifted_list.pop()
                group_id = lift_record.group_id
                user_id = lift_record.user_id

                db_manager.delete_ban_record(lift_record)  # 从数据库中删除禁言记录

                # Natural lifts are a separate structured-event producer, so
                # they must enter the same chat admission gate before any
                # member or group enrichment.  Keep the expired record
                # deleted even when policy rejects the notification.
                natural_lift_raw_message = {
                    "notice_type": NoticeType.group_ban,
                    "sub_type": NoticeType.GroupBan.lift_ban,
                    "group_id": group_id,
                    "user_id": user_id,
                    "operator_id": None,
                }
                if not await self._check_notice_chat_admission(
                    natural_lift_raw_message,
                    group_id,
                    user_id,
                    admission_user_id=user_id,
                ):
                    logger.warning(
                        "自然解除禁言 notice/system event 因聊天准入策略被丢弃: "
                        f"group_id={group_id}, user_id={user_id}"
                    )
                    await asyncio.sleep(0.5)
                    continue

                seg_message: Seg = await self.natural_lift(group_id, user_id)

                fetched_group_info = await get_group_info(self.server_connection, group_id)
                group_name: str = None
                if fetched_group_info:
                    group_name = fetched_group_info.get("group_name")
                else:
                    logger.warning("无法获取notice消息所在群的名称")
                group_info = GroupInfo(
                    platform=global_config.nachobot_server.platform_name,
                    group_id=group_id,
                    group_name=group_name,
                )

                seg_data = getattr(seg_message, "data", None) or {}
                lifted_user_info = seg_data.get("lifted_user_info") or {}
                target = None
                if user_id:
                    target_name = (
                        lifted_user_info.get("user_cardname")
                        or lifted_user_info.get("user_nickname")
                        or "QQ用户"
                    )
                    target = {
                        "user_id": str(user_id),
                        "name": target_name,
                    }

                system_event = build_system_event(
                    "qq.group_lift_ban",
                    actor=None,
                    target=target,
                    data={
                        "group_id": str(group_id),
                        "natural": True,
                    },
                )

                message_info: BaseMessageInfo = BaseMessageInfo(
                    platform=global_config.nachobot_server.platform_name,
                    message_id="notice",
                    time=time.time(),
                    user_info=None,
                    group_info=group_info,
                    template_info=None,
                    format_info=None,
                    additional_config={"system_event": system_event},
                )

                message_base: MessageBase = MessageBase(
                    message_info=message_info,
                    message_segment=seg_message,
                    raw_message=json.dumps(
                        {
                            "post_type": "notice",
                            "notice_type": "group_ban",
                            "sub_type": "lift_ban",
                            "group_id": group_id,
                            "user_id": user_id,
                            "operator_id": None,
                        }
                    ),
                )

                await self.put_notice(message_base)
                await asyncio.sleep(0.5)  # 确保队列处理间隔
            else:
                await asyncio.sleep(5)  # 每5秒检查一次

    async def natural_lift(self, group_id: int, user_id: int) -> Seg | None:
        if not group_id:
            logger.error("群ID不能为空，无法处理解除禁言通知")
            return None

        if user_id == 0:  # 理论上永远不会触发
            return Seg(
                type="notify",
                data={
                    "sub_type": "whole_lift_ban",
                    "lifted_user_info": None,
                },
            )

        user_nickname: str = "QQ用户"
        user_cardname: str = None
        fetched_member_info: dict = await get_member_info(self.server_connection, group_id, user_id)
        if fetched_member_info:
            user_nickname = fetched_member_info.get("nickname")
            user_cardname = fetched_member_info.get("card")

        lifted_user_info: UserInfo = UserInfo(
            platform=global_config.nachobot_server.platform_name,
            user_id=user_id,
            user_nickname=user_nickname,
            user_cardname=user_cardname,
        )

        return Seg(
            type="notify",
            data={
                "sub_type": "lift_ban",
                "lifted_user_info": lifted_user_info.to_dict(),
            },
        )

    async def auto_lift_detect(self) -> None:
        while True:
            # 清理self_muted_groups中过期的记录
            now = time.time()
            expired_groups = [gid for gid, end_time in self.self_muted_groups.items() if now >= end_time]
            for gid in expired_groups:
                del self.self_muted_groups[gid]
                logger.info(f"Bot自身在群 {gid} 的禁言已到期，自动恢复该群到核心的链路")

            if len(self.banned_list) == 0:
                await asyncio.sleep(5)
                continue
            for ban_record in self.banned_list:
                if ban_record.user_id == 0 or ban_record.lift_time == -1:
                    continue
                if ban_record.lift_time <= int(time.time()):
                    # 触发自然解除禁言
                    logger.info(f"检测到用户 {ban_record.user_id} 在群 {ban_record.group_id} 的禁言已解除")
                    self.lifted_list.append(ban_record)
                    self.banned_list.remove(ban_record)
            await asyncio.sleep(5)

    async def send_notice(self) -> None:
        """
        发送通知消息到Napcat
        """
        while True:
            if not unsuccessful_notice_queue.empty():
                to_be_send: MessageBase = await unsuccessful_notice_queue.get()
                try:
                    send_status = await message_send_instance.message_send(to_be_send)
                    if send_status:
                        unsuccessful_notice_queue.task_done()
                    else:
                        await unsuccessful_notice_queue.put(to_be_send)
                except Exception as e:
                    logger.error(f"发送通知消息失败: {str(e)}")
                    await unsuccessful_notice_queue.put(to_be_send)
                await asyncio.sleep(1)
                continue
            to_be_send: MessageBase = await notice_queue.get()
            try:
                send_status = await message_send_instance.message_send(to_be_send)
                if send_status:
                    notice_queue.task_done()
                else:
                    await unsuccessful_notice_queue.put(to_be_send)
            except Exception as e:
                logger.error(f"发送通知消息失败: {str(e)}")
                await unsuccessful_notice_queue.put(to_be_send)
            await asyncio.sleep(1)


notice_handler = NoticeHandler()
