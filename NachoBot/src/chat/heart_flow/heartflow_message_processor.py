import asyncio
import re
import traceback

from typing import Tuple, TYPE_CHECKING

from src.config.config import global_config
from src.chat.message_receive.message import MessageRecv
from src.chat.message_receive.storage import MessageStorage
from ncnk_message import get_system_event, system_event_fallback_text
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
    if get_system_event(message) is not None:
        message.interest_value = 1
        message.is_mentioned = False
        message.is_at = False
        message.reply_probability_boost = 0.0
        return 1.0, []

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
        """处理接收到的原始消息数据。

        system_event 与普通用户消息共享存储、Focus 路由和 HeartFlow 唤醒流程，
        但 system_event 没有 message sender，因此不得执行 Person 注册或其他
        普通用户身份语义。
        """
        try:
            system_event = get_system_event(message)
            # Screen updates and other empty ordinary messages remain dropped, but
            # a valid event must be retained with stable environmental text.
            if system_event is not None and not (message.processed_plain_text or "").strip():
                message.processed_plain_text = system_event_fallback_text(system_event)
            if not message.processed_plain_text and not (message.is_picid or message.is_emoji or message.is_voice):
                return

            userinfo = message.message_info.user_info
            chat = message.chat_stream
            is_system_event = system_event is not None

            # 兴趣度计算与更新。系统事件保留在环境上下文中，由普通会话的
            # Planner 或 planner_bypass 会话的 Replyer 继续处理。
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
                    if dispatch.interrupt_active:
                        heartflow_chat.signal_new_message(skip_interrupt=is_mentioned)
            else:
                heartflow_chat = await heartflow.get_or_create_heartflow_chat(chat.stream_id)
                if heartflow_chat is None:
                    raise RuntimeError(f"Cannot start chat runtime: {chat.stream_id}")
                heartflow_chat.signal_new_message(skip_interrupt=is_mentioned)

            # system_event 到这里已经完成：数据库持久化 + Focus 路由 + HeartFlow 唤醒。
            # actor/target 仅存在于 additional_config.system_event 元数据中，不是 sender，
            # 因此不能继续执行 mood/user reference/Person.register_person 等用户语义。
            if is_system_event:
                mes_name = chat.group_info.group_name if chat.group_info else "私聊"
                event_text = message.processed_plain_text or ""
                actor = system_event.get("actor")
                actor_name = actor.get("name") if isinstance(actor, dict) else None
                if actor_name:
                    actor_name = str(actor_name).strip()
                    if actor_name and not event_text.lstrip().startswith(actor_name):
                        event_text = f"{actor_name}{event_text}"
                logger.info(f"[{mes_name}][系统事件] {event_text}")
                return

            if global_config.mood.enable_mood:
                chat_mood = mood_manager.get_mood_by_chat_id(chat.stream_id)
                asyncio.create_task(chat_mood.update_mood_by_message(message))

            # 日志记录
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
                        processed_text = processed_text.replace(f"[picid:{picid}]", f"[图片：{image.description}]")
                    else:
                        processed_text = processed_text.replace(f"[picid:{picid}]", "[图片：网络不好，图片无法加载]")

            # 应用用户引用格式替换，将回复<aaa:bbb>和@<aaa:bbb>格式转换为可读格式
            processed_plain_text = replace_user_references(
                processed_text,
                message.message_info.platform,  # type: ignore
                replace_bot_name=True,
            )

            logger.info(f"[{mes_name}]{userinfo.user_nickname}:{processed_plain_text}")  # type: ignore

            _ = Person.register_person(
                platform=message.message_info.platform,  # type: ignore
                user_id=message.message_info.user_info.user_id,  # type: ignore
                nickname=userinfo.user_nickname,  # type: ignore
                group_id=message.message_info.group_info.group_id if message.message_info.group_info else None,
                group_cardname=userinfo.user_cardname,
            )

        except Exception as e:
            logger.error(f"消息处理失败: {e}")
            print(traceback.format_exc())
