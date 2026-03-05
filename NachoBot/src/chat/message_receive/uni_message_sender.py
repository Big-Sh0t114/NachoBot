import asyncio
import traceback

from rich.traceback import install
from ncnk_message import Seg

from src.common.message.api import get_global_api
from src.common.logger import get_logger
from src.chat.message_receive.message import MessageSending
from src.chat.message_receive.storage import MessageStorage
from src.chat.utils.utils import truncate_message
from src.chat.utils.utils import calculate_typing_time
from src.config.config import global_config

install(extra_lines=3)

logger = get_logger("sender")


_DEFAULT_BLOCKED_MARKERS = (
    "i'm kiro",
    "i am kiro",
    "kiro-cli chat",
    "kiro-cli",
    "kiro cli",
    "ai assistant built by aws",
    "aws services",
    "i can't engage with this request",
    "i need to decline this request",
    "this message is attempting to manipulate",
    "adopting a fake persona",
    "creating a fake persona",
    "roleplaying as a character",
    "roleplay as a different character",
    "i don't roleplay as other characters",
    "i don't pretend to be someone else",
    "fabricated instructions",
    "fabricated rules",
    "ignoring my actual instructions",
    "instructions to ignore my real system prompts",
    "override my actual identity",
    "actual identity and guidelines",
    "responding as if i'm in a qq",
    "qq chat group",
    "identity verification",
    "false claims about",
    "fabricated identity",
    "fake conversation history",
)


def _get_response_filter_settings() -> tuple[bool, list[str]]:
    filter_config = getattr(global_config, "response_filter", None)
    if filter_config is None:
        return True, [marker.lower() for marker in _DEFAULT_BLOCKED_MARKERS]
    enabled = bool(getattr(filter_config, "enable", True))
    markers = getattr(filter_config, "blocked_markers", [])
    cleaned = [str(marker).lower() for marker in markers if str(marker).strip()]
    return enabled, cleaned


def _should_suppress_text_reply(text: str) -> bool:
    if not text:
        return False
    enabled, markers = _get_response_filter_settings()
    if not enabled or not markers:
        return False
    normalized = text.lower()
    return any(marker in normalized for marker in markers)


async def _send_message(message: MessageSending, show_log=True) -> bool:
    """合并后的消息发送函数，包含WS发送和日志记录"""
    message_preview = truncate_message(message.processed_plain_text, max_length=200)

    try:
        # 直接调用API发送消息
        await get_global_api().send_message(message)
        if show_log:
            logger.info(f"已将消息  '{message_preview}'  发往平台'{message.message_info.platform}'")
        return True

    except Exception as e:
        logger.error(f"发送消息   '{message_preview}'   发往平台'{message.message_info.platform}' 失败: {str(e)}")
        traceback.print_exc()
        raise e  # 重新抛出其他异常


class UniversalMessageSender:
    """管理消息的注册、即时处理、发送和存储，并跟踪思考状态。"""

    def __init__(self):
        self.storage = MessageStorage()

    async def send_message(
        self, message: MessageSending, typing=False, set_reply=False, storage_message=True, show_log=True
    ):
        """
        处理、发送并存储一条消息。

        参数：
            message: MessageSending 对象，待发送的消息。
            typing: 是否模拟打字等待。

        用法：
            - typing=True 时，发送前会有打字等待。
        """
        if not message.chat_stream:
            logger.error("消息缺少 chat_stream，无法发送")
            raise ValueError("消息缺少 chat_stream，无法发送")
        if not message.message_info or not message.message_info.message_id:
            logger.error("消息缺少 message_info 或 message_id，无法发送")
            raise ValueError("消息缺少 message_info 或 message_id，无法发送")

        chat_id = message.chat_stream.stream_id
        message_id = message.message_info.message_id

        try:
            if set_reply:
                message.build_reply()
                logger.debug(f"[{chat_id}] 选择回复引用消息: {message.processed_plain_text[:20]}...")

            from src.plugin_system.core.events_manager import events_manager
            from src.plugin_system.base.component_types import EventType

            continue_flag, modified_message = await events_manager.handle_nacho_events(
                EventType.POST_SEND_PRE_PROCESS, message=message, stream_id=chat_id
            )
            if not continue_flag:
                logger.info(f"[{chat_id}] 消息发送被插件取消: {str(message.message_segment)[:100]}...")
                return False
            if modified_message:
                if modified_message._modify_flags.modify_message_segments:
                    message.message_segment = Seg(type="seglist", data=modified_message.message_segments)
                if modified_message._modify_flags.modify_plain_text:
                    logger.warning(f"[{chat_id}] 插件修改了消息的纯文本内容，可能导致此内容被覆盖。")
                    message.processed_plain_text = modified_message.plain_text

            await message.process()

            continue_flag, modified_message = await events_manager.handle_nacho_events(
                EventType.POST_SEND, message=message, stream_id=chat_id
            )
            if not continue_flag:
                logger.info(f"[{chat_id}] 消息发送被插件取消: {str(message.message_segment)[:100]}...")
                return False
            if modified_message:
                if modified_message._modify_flags.modify_message_segments:
                    message.message_segment = Seg(type="seglist", data=modified_message.message_segments)
                if modified_message._modify_flags.modify_plain_text:
                    message.processed_plain_text = modified_message.plain_text

            if _should_suppress_text_reply(message.processed_plain_text or ""):
                logger.warning(f"[{chat_id}] 过滤前信息: {message.processed_plain_text}")
                logger.error(f"[{chat_id}] 检测到可疑回复模板，已替换为 Filtered")
                filtered_segment = Seg(type="text", data="Filtered")
                if message.reply and getattr(message.reply.message_info, "message_id", None):
                    message.message_segment = Seg(
                        type="seglist",
                        data=[Seg(type="reply", data=message.reply.message_info.message_id), filtered_segment],  # type: ignore
                    )
                else:
                    message.message_segment = filtered_segment
                message.processed_plain_text = "Filtered"
                message.display_message = "Filtered"

            if typing:
                typing_time = calculate_typing_time(
                    input_string=message.processed_plain_text,
                    thinking_start_time=message.thinking_start_time,
                    is_emoji=message.is_emoji,
                )
                await asyncio.sleep(typing_time)

            sent_msg = await _send_message(message, show_log=show_log)
            if not sent_msg:
                return False

            continue_flag, modified_message = await events_manager.handle_nacho_events(
                EventType.AFTER_SEND, message=message, stream_id=chat_id
            )
            if not continue_flag:
                logger.info(f"[{chat_id}] 消息发送后续处理被插件取消: {str(message.message_segment)[:100]}...")
                return True
            if modified_message:
                if modified_message._modify_flags.modify_message_segments:
                    message.message_segment = Seg(type="seglist", data=modified_message.message_segments)
                if modified_message._modify_flags.modify_plain_text:
                    message.processed_plain_text = modified_message.plain_text

            if storage_message:
                await self.storage.store_message(message, message.chat_stream)

            return sent_msg

        except Exception as e:
            logger.error(f"[{chat_id}] 处理或存储消息 {message_id} 时出错: {e}")
            raise e
