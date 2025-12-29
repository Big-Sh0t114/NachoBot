import time
import json
import asyncio
import websockets as Server
from typing import Tuple, Optional

from src.logger import logger
from src.config import global_config
from src.database import BanUser, db_manager, is_identical
from . import NoticeType, ACCEPT_FORMAT
from .message_sending import message_send_instance
from .message_handler import message_handler
from maim_message import FormatInfo, UserInfo, GroupInfo, Seg, BaseMessageInfo, MessageBase

from src.utils import (
    get_group_info,
    get_member_info,
    get_self_info,
    get_stranger_info,
    read_ban_list,
)

notice_queue: asyncio.Queue[MessageBase] = asyncio.Queue(maxsize=100)
unsuccessful_notice_queue: asyncio.Queue[MessageBase] = asyncio.Queue(maxsize=3)


class NoticeHandler:
    banned_list: list[BanUser] = []  # 当前仍在禁言中的用户列表
    lifted_list: list[BanUser] = []  # 已经自然解除禁言

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

    async def handle_notice(self, raw_message: dict) -> None:
        notice_type = raw_message.get("notice_type")
        # message_time: int = raw_message.get("time")
        message_time: float = time.time()  # 应可乐要求，现在是float了

        group_id = raw_message.get("group_id")
        user_id = raw_message.get("user_id")
        target_id = raw_message.get("target_id")

        handled_message: Seg = None
        user_info: UserInfo = None
        system_notice: bool = False

        match notice_type:
            case NoticeType.friend_recall:
                logger.info("好友撤回一条消息")
                logger.info(f"撤回消息ID：{raw_message.get('message_id')}, 撤回时间：{raw_message.get('time')}")
                logger.warning("暂时不支持撤回消息处理")
            case NoticeType.group_recall:
                logger.info("群内用户撤回一条消息")
                logger.info(f"撤回消息ID：{raw_message.get('message_id')}, 撤回时间：{raw_message.get('time')}")
                logger.warning("暂时不支持撤回消息处理")
            case NoticeType.notify:
                sub_type = raw_message.get("sub_type")
                match sub_type:
                    case NoticeType.Notify.poke:
                        if global_config.chat.enable_poke and await message_handler.check_allow_to_chat(
                            user_id, group_id, False, False
                        ):
                            logger.info("处理戳一戳消息")
                            handled_message, user_info = await self.handle_poke_notify(raw_message, group_id, user_id)
                        else:
                            logger.warning("戳一戳消息被禁用，取消戳一戳处理")
                    case _:
                        logger.warning(f"不支持的notify类型: {notice_type}.{sub_type}")
            case NoticeType.group_ban:
                sub_type = raw_message.get("sub_type")
                match sub_type:
                    case NoticeType.GroupBan.ban:
                        if not await message_handler.check_allow_to_chat(user_id, group_id, True, False):
                            return None
                        logger.info("处理群禁言")
                        handled_message, user_info = await self.handle_ban_notify(raw_message, group_id)
                        system_notice = True
                    case NoticeType.GroupBan.lift_ban:
                        if not await message_handler.check_allow_to_chat(user_id, group_id, True, False):
                            return None
                        logger.info("处理解除群禁言")
                        handled_message, user_info = await self.handle_lift_ban_notify(raw_message, group_id)
                        system_notice = True
                    case _:
                        logger.warning(f"不支持的group_ban类型: {notice_type}.{sub_type}")
            case _:
                logger.warning(f"不支持的notice类型: {notice_type}")
                return None
        if not handled_message or not user_info:
            logger.warning("notice处理失败或不支持")
            return None

        group_info: GroupInfo = None
        if group_id:
            fetched_group_info = await get_group_info(self.server_connection, group_id)
            group_name: str = None
            if fetched_group_info:
                group_name = fetched_group_info.get("group_name")
            else:
                logger.warning("无法获取notice消息所在群的名称")
            group_info = GroupInfo(
                platform=global_config.maibot_server.platform_name,
                group_id=group_id,
                group_name=group_name,
            )

        message_info: BaseMessageInfo = BaseMessageInfo(
            platform=global_config.maibot_server.platform_name,
            message_id="notice",
            time=message_time,
            user_info=user_info,
            group_info=group_info,
            template_info=None,
            format_info=FormatInfo(
                content_format=["text", "notify"],
                accept_format=ACCEPT_FORMAT,
            ),
            additional_config={"target_id": target_id},  # 在这里塞了一个target_id，方便mmc那边知道被戳的人是谁
        )

        message_base: MessageBase = MessageBase(
            message_info=message_info,
            message_segment=handled_message,
            raw_message=json.dumps(raw_message),
        )

        if system_notice:
            await self.put_notice(message_base)
        else:
            logger.info("发送到Maibot处理通知信息")
            await message_send_instance.message_send(message_base)

    async def handle_poke_notify(
        self, raw_message: dict, group_id: int, user_id: int
    ) -> Tuple[Seg | None, UserInfo | None]:
        # sourcery skip: merge-comparisons, merge-duplicate-blocks, remove-redundant-if, remove-unnecessary-else, swap-if-else-branches
        self_info: dict = await get_self_info(self.server_connection)

        if not self_info:
            logger.error("自身信息获取失败")
            return None, None

        self_id = raw_message.get("self_id")
        target_id = raw_message.get("target_id")
        target_name: str = None
        raw_info: list = raw_message.get("raw_info")

        if group_id:
            user_qq_info: dict = await get_member_info(self.server_connection, group_id, user_id)
        else:
            user_qq_info: dict = await get_stranger_info(self.server_connection, user_id)
        if user_qq_info:
            user_name = user_qq_info.get("nickname")
            user_cardname = user_qq_info.get("card")
        else:
            user_name = "QQ用户"
            user_cardname = "QQ用户"
            logger.info("无法获取戳一戳对方的用户昵称")

        # 计算Seg
        if self_id == target_id:
            display_name = ""
            target_name = self_info.get("nickname")

        elif self_id == user_id:
            # 让ada不发送麦麦戳别人的消息
            return None, None

        else:
            # 老实说这一步判定没啥意义，毕竟私聊是没有其他人之间的戳一戳，但是感觉可以有这个判定来强限制群聊环境
            if group_id:
                fetched_member_info: dict = await get_member_info(self.server_connection, group_id, target_id)
                if fetched_member_info:
                    target_name = fetched_member_info.get("nickname")
                else:
                    target_name = "QQ用户"
                    logger.info("无法获取被戳一戳方的用户昵称")
                display_name = user_name
            else:
                return None, None

        first_txt: str = "戳了戳"
        second_txt: str = ""
        try:
            first_txt = raw_info[2].get("txt", "戳了戳")
            second_txt = raw_info[4].get("txt", "")
        except Exception as e:
            logger.warning(f"解析戳一戳消息失败: {str(e)}，将使用默认文本")

        user_info: UserInfo = UserInfo(
            platform=global_config.maibot_server.platform_name,
            user_id=user_id,
            user_nickname=user_name,
            user_cardname=user_cardname,
        )

        seg_data: Seg = Seg(
            type="text",
            data=f"{display_name}{first_txt}{target_name}{second_txt}（这是QQ的一个功能，用于提及某人，但没那么明显）",
        )
        return seg_data, user_info

    async def handle_ban_notify(self, raw_message: dict, group_id: int) -> Tuple[Seg, UserInfo] | Tuple[None, None]:
        if not group_id:
            logger.error("群ID不能为空，无法处理禁言通知")
            return None, None

        # 计算user_info
        operator_id = raw_message.get("operator_id")
        operator_nickname: str = None
        operator_cardname: str = None

        member_info: dict = await get_member_info(self.server_connection, group_id, operator_id)
        if member_info:
            operator_nickname = member_info.get("nickname")
            operator_cardname = member_info.get("card")
        else:
            logger.warning("无法获取禁言执行者的昵称，消息可能会无效")
            operator_nickname = "QQ用户"

        operator_info: UserInfo = UserInfo(
            platform=global_config.maibot_server.platform_name,
            user_id=operator_id,
            user_nickname=operator_nickname,
            user_cardname=operator_cardname,
        )

        # 计算Seg
        user_id = raw_message.get("user_id")
        banned_user_info: UserInfo = None
        user_nickname: str = "QQ用户"
        user_cardname: str = None
        sub_type: str = None

        duration = raw_message.get("duration")
        if duration is None:
            logger.error("禁言时长不能为空，无法处理禁言通知")
            return None, None

        if user_id == 0:  # 为全体禁言
            sub_type: str = "whole_ban"
            self._ban_operation(group_id)
        else:  # 为单人禁言
            # 获取被禁言人的信息
            sub_type: str = "ban"
            fetched_member_info: dict = await get_member_info(self.server_connection, group_id, user_id)
            if fetched_member_info:
                user_nickname = fetched_member_info.get("nickname")
                user_cardname = fetched_member_info.get("card")
            banned_user_info: UserInfo = UserInfo(
                platform=global_config.maibot_server.platform_name,
                user_id=user_id,
                user_nickname=user_nickname,
                user_cardname=user_cardname,
            )
            self._ban_operation(group_id, user_id, int(time.time() + duration))

        seg_data: Seg = Seg(
            type="notify",
            data={
                "sub_type": sub_type,
                "duration": duration,
                "banned_user_info": banned_user_info.to_dict() if banned_user_info else None,
            },
        )

        return seg_data, operator_info

    async def handle_lift_ban_notify(
        self, raw_message: dict, group_id: int
    ) -> Tuple[Seg, UserInfo] | Tuple[None, None]:
        if not group_id:
            logger.error("群ID不能为空，无法处理解除禁言通知")
            return None, None

        # 计算user_info
        operator_id = raw_message.get("operator_id")
        operator_nickname: str = None
        operator_cardname: str = None

        member_info: dict = await get_member_info(self.server_connection, group_id, operator_id)
        if member_info:
            operator_nickname = member_info.get("nickname")
            operator_cardname = member_info.get("card")
        else:
            logger.warning("无法获取解除禁言执行者的昵称，消息可能会无效")
            operator_nickname = "QQ用户"

        operator_info: UserInfo = UserInfo(
            platform=global_config.maibot_server.platform_name,
            user_id=operator_id,
            user_nickname=operator_nickname,
            user_cardname=operator_cardname,
        )

        # 计算Seg
        sub_type: str = None
        user_nickname: str = "QQ用户"
        user_cardname: str = None
        lifted_user_info: UserInfo = None

        user_id = raw_message.get("user_id")
        if user_id == 0:  # 全体禁言解除
            sub_type = "whole_lift_ban"
            self._lift_operation(group_id)
        else:  # 单人禁言解除
            sub_type = "lift_ban"
            # 获取被解除禁言人的信息
            fetched_member_info: dict = await get_member_info(self.server_connection, group_id, user_id)
            if fetched_member_info:
                user_nickname = fetched_member_info.get("nickname")
                user_cardname = fetched_member_info.get("card")
            else:
                logger.warning("无法获取解除禁言消息发送者的昵称，消息可能会无效")
            lifted_user_info: UserInfo = UserInfo(
                platform=global_config.maibot_server.platform_name,
                user_id=user_id,
                user_nickname=user_nickname,
                user_cardname=user_cardname,
            )
            self._lift_operation(group_id, user_id)

        seg_data: Seg = Seg(
            type="notify",
            data={
                "sub_type": sub_type,
                "lifted_user_info": lifted_user_info.to_dict() if lifted_user_info else None,
            },
        )
        return seg_data, operator_info

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

                seg_message: Seg = await self.natural_lift(group_id, user_id)

                fetched_group_info = await get_group_info(self.server_connection, group_id)
                group_name: str = None
                if fetched_group_info:
                    group_name = fetched_group_info.get("group_name")
                else:
                    logger.warning("无法获取notice消息所在群的名称")
                group_info = GroupInfo(
                    platform=global_config.maibot_server.platform_name,
                    group_id=group_id,
                    group_name=group_name,
                )

                message_info: BaseMessageInfo = BaseMessageInfo(
                    platform=global_config.maibot_server.platform_name,
                    message_id="notice",
                    time=time.time(),
                    user_info=None,  # 自然解除禁言没有操作者
                    group_info=group_info,
                    template_info=None,
                    format_info=None,
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
                            "operator_id": None,  # 自然解除禁言没有操作者
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
            platform=global_config.maibot_server.platform_name,
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
