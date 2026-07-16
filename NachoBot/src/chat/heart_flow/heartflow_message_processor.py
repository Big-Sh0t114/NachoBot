import asyncio
import re
import traceback

from typing import Tuple, TYPE_CHECKING

from src.config.config import global_config
from src.chat.message_receive.message import MessageRecv
from src.chat.message_receive.storage import MessageStorage
from src.chat.heart_flow.heartflow import heartflow
from src.chat.focus.coordinator import focus_coordinator
from src.chat.utils.utils import is_mentioned_bot_in_message
from src.chat.utils.chat_message_builder import replace_user_references
from src.common.logger import get_logger
from src.mood.mood_manager import mood_manager
from src.person_info.person_info import Person
from src.common.database.database_model import Images

if TYPE_CHECKING:
    from src.chat.heart_flow.heartFC_chat import HeartFChatting

logger = get_logger("chat")


async def _calculate_interest(message: MessageRecv) -> Tuple[float, list[str]]:
    """计算消息的兴趣度

    Args:
        message: 待处理的消息对象

    Returns:
        Tuple[float, bool, list[str]]: (兴趣度, 是否被提及, 关键词)
    """
    if message.is_picid or message.is_emoji:
        return 0.0, []

    is_mentioned, is_at, reply_probability_boost = is_mentioned_bot_in_message(message)
    # interested_rate = 0.0
    keywords = []

    message.interest_value = 1
    message.is_mentioned = is_mentioned
    message.is_at = is_at
    message.reply_probability_boost = reply_probability_boost

    if message.processed_plain_text and message.processed_plain_text.strip().startswith("[文件:"):
        message.interest_value = 1
        message.is_mentioned = True
        return 1.0, []

    return 1, keywords


class HeartFCMessageReceiver:
    """心流处理器，负责处理接收到的消息并计算兴趣度"""

    def __init__(self):
        """初始化心流处理器，创建消息存储实例"""
        self.storage = MessageStorage()

    async def process_message(self, message: MessageRecv) -> None:
        """处理接收到的原始消息数据

        主要流程:
        1. 消息解析与初始化
        2. 消息缓冲处理
        3. 过滤检查
        4. 兴趣度计算
        5. 关系处理

        Args:
            message_data: 原始消息字符串
        """
        try:
            # [Fix] Drop empty messages (e.g. Screen Updates processed by message.py)
            if not message.processed_plain_text and not (message.is_picid or message.is_emoji or message.is_voice):
                return

            # 1. 消息解析与初始化
            userinfo = message.message_info.user_info
            chat = message.chat_stream

            # 2. 兴趣度计算与更新
            _, keywords = await _calculate_interest(message)

            stored_ref = await self.storage.store_message(message, chat)
            dispatch = await focus_coordinator.route_message(message, stored_ref)

            heartflow_chat = None

            # 推送新消息通知，若 Planner 正在执行则触发打断（@提及消息不触发打断）
            is_mentioned = getattr(message, "is_mentioned", False) or getattr(message, "is_at", False)
            if dispatch.managed:
                if dispatch.woke_active and dispatch.active_chat_id:
                    heartflow_chat = await heartflow.get_or_create_heartflow_chat(dispatch.active_chat_id)
                    if heartflow_chat is None:
                        raise RuntimeError(f"Cannot start Focus active chat: {dispatch.active_chat_id}")
                    heartflow_chat.signal_new_message(skip_interrupt=is_mentioned)
            else:
                heartflow_chat = await heartflow.get_or_create_heartflow_chat(chat.stream_id)
                if heartflow_chat is None:
                    raise RuntimeError(f"Cannot start chat runtime: {chat.stream_id}")
                heartflow_chat.signal_new_message(skip_interrupt=is_mentioned)

            if global_config.mood.enable_mood:
                chat_mood = mood_manager.get_mood_by_chat_id(chat.stream_id)
                asyncio.create_task(chat_mood.update_mood_by_message(message))

            # 3. 日志记录
            mes_name = chat.group_info.group_name if chat.group_info else "私聊"

            # 用这个pattern截取出id部分，picid是一个list，并替换成对应的图片描述
            picid_pattern = r"\[picid:([^\]]+)\]"
            picid_list = re.findall(picid_pattern, message.processed_plain_text)

            # 创建替换后的文本
            processed_text = message.processed_plain_text
            if picid_list:
                for picid in picid_list:
                    image = Images.get_or_none(Images.image_id == picid)
                    if image and image.description:
                        # 将[picid:xxxx]替换成图片描述
                        processed_text = processed_text.replace(f"[picid:{picid}]", f"[图片：{image.description}]")
                    else:
                        # 如果没有找到图片描述，则移除[picid:xxxx]标记
                        processed_text = processed_text.replace(f"[picid:{picid}]", "[图片：网络不好，图片无法加载]")

            # 应用用户引用格式替换，将回复<aaa:bbb>和@<aaa:bbb>格式转换为可读格式
            processed_plain_text = replace_user_references(
                processed_text,
                message.message_info.platform,  # type: ignore
                replace_bot_name=True,
            )
            # if not processed_plain_text:
            # print(message)

            logger.info(f"[{mes_name}]{userinfo.user_nickname}:{processed_plain_text}")  # type: ignore

            _ = Person.register_person(
                platform=message.message_info.platform,  # type: ignore
                user_id=message.message_info.user_info.user_id,  # type: ignore
                nickname=userinfo.user_nickname,  # type: ignore
            )

        except Exception as e:
            logger.error(f"消息处理失败: {e}")
            print(traceback.format_exc())
