"""
发送API模块

专门负责发送各种类型的消息，采用标准Python包设计模式

使用方式：
    from src.plugin_system.apis import send_api

    # 方式1：直接使用stream_id（推荐）
    await send_api.text_to_stream("hello", stream_id)
    await send_api.emoji_to_stream(emoji_base64, stream_id)
    await send_api.custom_to_stream("video", video_data, stream_id)

    # 方式2：使用群聊/私聊指定函数
    await send_api.text_to_group("hello", "123456")
    await send_api.text_to_user("hello", "987654")

    # 方式3：使用通用custom_message函数
    await send_api.custom_message("video", video_data, "123456", True)
"""

import traceback
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union, Dict, List, TYPE_CHECKING, Tuple

from src.common.logger import get_logger
from src.common.data_models.message_data_model import ReplyContentType
from src.config.config import global_config
from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.message_receive.uni_message_sender import UniversalMessageSender
from src.chat.message_receive.message import MessageSending, MessageRecv
from src.chat.focus.coordinator import current_context_lease, focus_coordinator
from src.chat.focus.models import EffectKind, StaleFocusLeaseError
from ncnk_message import Seg, UserInfo, MessageBase, BaseMessageInfo

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages
    from src.common.data_models.message_data_model import ReplySetModel, ReplyContent, ForwardNode

logger = get_logger("send_api")


class SendStatus(str, Enum):
    """可区分真实投递、策略抑制与失败的发送结果。"""

    DELIVERED = "delivered"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    STALE_LEASE = "stale_lease"


@dataclass(frozen=True)
class SendReceipt:
    status: SendStatus
    stream_id: str
    message_id: Optional[str] = None
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return self.status is SendStatus.DELIVERED


# =============================================================================
# 内部实现函数（不暴露给外部）
# =============================================================================


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


def _get_response_filter_settings() -> tuple[bool, List[str]]:
    filter_config = getattr(global_config, "response_filter", None)
    if filter_config is None:
        return True, [marker.lower() for marker in _DEFAULT_BLOCKED_MARKERS]
    enabled = bool(getattr(filter_config, "enable", True))
    markers = getattr(filter_config, "blocked_markers", [])
    cleaned = [str(marker).lower() for marker in markers if str(marker).strip()]
    return enabled, cleaned


def _should_suppress_text_reply(text: str, enabled: Optional[bool] = None, markers: Optional[List[str]] = None) -> bool:
    if not text:
        return False
    if enabled is None or markers is None:
        enabled, markers = _get_response_filter_settings()
    if not enabled or not markers:
        return False
    normalized = text.lower()
    return any(marker in normalized for marker in markers)


def _should_suppress_reply_set(reply_set: "ReplySetModel") -> bool:
    enabled, markers = _get_response_filter_settings()
    if not enabled or not markers:
        return False
    aggregated_texts: List[str] = []
    for reply_content in reply_set.reply_data:
        content_type = reply_content.content_type
        if content_type == ReplyContentType.TEXT:
            text_content = str(reply_content.content)
            aggregated_texts.append(text_content)
            if _should_suppress_text_reply(text_content, enabled=enabled, markers=markers):
                return True
        elif content_type == ReplyContentType.HYBRID:
            hybrid_message_list_data: List["ReplyContent"] = reply_content.content  # type: ignore
            if not isinstance(hybrid_message_list_data, list):
                continue
            for sub_content in hybrid_message_list_data:
                if sub_content.content_type == ReplyContentType.TEXT:
                    text_content = str(sub_content.content)
                    aggregated_texts.append(text_content)
                    if _should_suppress_text_reply(text_content, enabled=enabled, markers=markers):
                        return True
    if aggregated_texts and _should_suppress_text_reply(" ".join(aggregated_texts), enabled=enabled, markers=markers):
        return True
    return False


def _get_suppressed_reply_texts(target_stream) -> List[str]:
    """Return exact reply texts suppressed by the adapter-declared policy."""
    try:
        context = getattr(target_stream, "context", None)
        last_message = getattr(context, "message", None) if context else None
        if not last_message:
            return []
        message_info = getattr(last_message, "message_info", None)
        additional = getattr(message_info, "additional_config", None)
        if not isinstance(additional, dict):
            return []

        policy = additional.get("reply_policy")
        if isinstance(policy, dict):
            try:
                schema_version = int(policy.get("schema_version", 1))
            except (TypeError, ValueError):
                return []
            if schema_version != 1:
                return []
            texts = policy.get("suppress_exact_texts") or []
        else:
            # Transitional compatibility for adapters using the original fields.
            if not additional.get("silent_reply"):
                return []
            texts = additional.get("silent_reply_texts") or []

        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, (list, tuple, set)):
            return []
        cleaned = (str(text).strip() for text in texts)
        return list(dict.fromkeys(text for text in cleaned if text))
    except Exception:
        return []


def _should_suppress_reply_by_policy(target_stream, text: str) -> bool:
    if not text:
        return False
    texts = _get_suppressed_reply_texts(target_stream)
    if not texts:
        return False
    return text in texts


async def _send_to_target_receipt(
    message_segment: Seg,
    stream_id: str,
    display_message: str = "",
    typing: bool = False,
    set_reply: bool = False,
    reply_message: Optional["DatabaseMessages"] = None,
    storage_message: bool = True,
    show_log: bool = True,
    selected_expressions: Optional[List[int]] = None,
) -> SendReceipt:
    """发送聊天生成的回复，并返回经过 Focus 围栏的真实投递结果。

    该入口只供需要 ACK/结算回复上下文的聊天生成链路使用。插件、
    hook 和系统通知使用 legacy bool API；它们不是 Focus 回合副作用，
    不应受 active chat 租约限制。
    """
    try:
        async with focus_coordinator.effect_permit(
            None,
            EffectKind.SEND,
            target_chat_id=stream_id,
        ):
            return await _send_to_target_receipt_permitted(
                message_segment=message_segment,
                stream_id=stream_id,
                display_message=display_message,
                typing=typing,
                set_reply=set_reply,
                reply_message=reply_message,
                storage_message=storage_message,
                show_log=show_log,
                selected_expressions=selected_expressions,
            )
    except StaleFocusLeaseError as exc:
        logger.warning(f"[SendAPI] Focus 租约失效，拒绝发送到 {stream_id}: {exc}")
        return SendReceipt(SendStatus.STALE_LEASE, stream_id, detail=str(exc))


async def _send_to_target_receipt_permitted(
    message_segment: Seg,
    stream_id: str,
    display_message: str = "",
    typing: bool = False,
    set_reply: bool = False,
    reply_message: Optional["DatabaseMessages"] = None,
    storage_message: bool = True,
    show_log: bool = True,
    selected_expressions: Optional[List[int]] = None,
) -> SendReceipt:
    """向指定目标发送消息的内部实现

    Args:
        message_segment:
        stream_id: 目标流ID
        display_message: 显示消息
        typing: 是否模拟打字等待
        reply_to: 回复消息，格式为"发送者:消息内容"
        storage_message: 是否存储消息到数据库
        show_log: 发送是否显示日志

    Returns:
        bool: 是否发送成功
    """
    try:
        if set_reply and not reply_message:
            logger.warning("[SendAPI] 使用引用回复，但未提供回复消息")
            return SendReceipt(SendStatus.FAILED, stream_id, detail="missing_reply_message")

        text_data: Optional[str] = None
        if message_segment.type == "text":
            text_data = str(message_segment.data)
            if _should_suppress_text_reply(text_data):
                logger.warning(f"[SendAPI] 过滤前信息: {text_data}")
                logger.error("[SendAPI] 检测到可疑回复模板，已替换为 Filtered")
                message_segment = Seg(type="text", data="Filtered")
                text_data = "Filtered"

        if show_log:
            logger.debug(f"[SendAPI] 发送{message_segment.type}消息到 {stream_id}")

        # 查找目标聊天流
        target_stream = get_chat_manager().get_stream(stream_id)
        if not target_stream:
            logger.error(f"[SendAPI] 未找到聊天流: {stream_id}")
            return SendReceipt(SendStatus.FAILED, stream_id, detail="stream_not_found")
        if text_data is not None and _should_suppress_reply_by_policy(target_stream, text_data):
            logger.info("[SendAPI] 本次回复已被适配器回复策略抑制")
            return SendReceipt(SendStatus.SUPPRESSED, stream_id, detail="reply_policy")

        # 创建发送器
        message_sender = UniversalMessageSender()

        # 生成消息ID
        current_time = time.time()
        message_id = f"send_api_{int(current_time * 1000)}"

        # 构建机器人用户信息
        bot_user_info = UserInfo(
            user_id=global_config.bot.qq_account,
            user_nickname=global_config.bot.nickname,
            platform=target_stream.platform,
        )

        reply_to_platform_id = ""
        anchor_message: Union["MessageRecv", None] = None
        if reply_message:
            reply_chat_id = str(getattr(reply_message, "chat_id", "") or "")
            if reply_chat_id and reply_chat_id != str(stream_id):
                logger.error(
                    f"[SendAPI] 拒绝跨流引用回复: reply_chat_id={reply_chat_id}, target={stream_id}"
                )
                return SendReceipt(SendStatus.FAILED, stream_id, detail="cross_stream_reply")
            anchor_message = db_message_to_message_recv(reply_message)
            logger.info(f"[SendAPI] 找到匹配的回复消息，发送者: {anchor_message.message_info.user_info.user_id}")  # type: ignore
            if anchor_message:
                anchor_message.update_chat_stream(target_stream)
                assert anchor_message.message_info.user_info, "用户信息缺失"
                reply_to_platform_id = (
                    f"{anchor_message.message_info.platform}:{anchor_message.message_info.user_info.user_id}"
                )

        # 构建发送消息对象
        bot_message = MessageSending(
            message_id=message_id,
            chat_stream=target_stream,
            bot_user_info=bot_user_info,
            sender_info=target_stream.user_info,
            message_segment=message_segment,
            display_message=display_message,
            reply=anchor_message,
            is_head=True,
            is_emoji=(message_segment.type == "emoji"),
            thinking_start_time=current_time,
            reply_to=reply_to_platform_id,
            selected_expressions=selected_expressions,
        )

        # 兼容概率转语音：写入当前TTS语种元数据，供后端TTS概率链路读取
        try:
            from src.plugins.built_in.tts_plugin.plugin import TTSAction

            target_id = None
            if target_stream.group_info and getattr(target_stream.group_info, "group_id", None):
                target_id = str(target_stream.group_info.group_id)
            elif target_stream.user_info and getattr(target_stream.user_info, "user_id", None):
                target_id = str(target_stream.user_info.user_id)

            tts_lang = await TTSAction.get_language_for_chat(chat_id=stream_id, target_id=target_id)
            # BaseMessageInfo 可能已有 additional_config，确保存在并写入
            if not getattr(bot_message.message_info, "additional_config", None):
                bot_message.message_info.additional_config = {}
            if tts_lang:
                bot_message.message_info.additional_config.setdefault("tts_language", tts_lang)
        except Exception as e:
            logger.debug(f"[SendAPI] 写入TTS语种元数据失败: {e}")

        # 发送消息
        sent_msg = await message_sender.send_message(
            bot_message,
            typing=typing,
            set_reply=set_reply,
            storage_message=storage_message,
            show_log=show_log,
        )

        if sent_msg:
            logger.debug(f"[SendAPI] 成功发送消息到 {stream_id}")
            delivered_message_id = str(
                getattr(getattr(sent_msg, "message_info", None), "message_id", None) or message_id
            )
            return SendReceipt(SendStatus.DELIVERED, stream_id, message_id=delivered_message_id)
        else:
            logger.error("[SendAPI] 发送消息失败")
            return SendReceipt(SendStatus.FAILED, stream_id, message_id=message_id, detail="adapter_failed")

    except Exception as e:
        logger.error(f"[SendAPI] 发送消息时出错: {e}")
        traceback.print_exc()
        return SendReceipt(SendStatus.FAILED, stream_id, detail=type(e).__name__)


async def _send_to_target(
    message_segment: Seg,
    stream_id: str,
    display_message: str = "",
    typing: bool = False,
    set_reply: bool = False,
    reply_message: Optional["DatabaseMessages"] = None,
    storage_message: bool = True,
    show_log: bool = True,
    selected_expressions: Optional[List[int]] = None,
) -> bool:
    """通用发送入口；仅围栏当前 Focus 回合产生的副作用。

    未绑定 Focus 租约的插件、hook 和系统通知可向任意已注册聊天流
    发送，不受 active chat 限制。若调用发生在 Focus 回合上下文内，
    则继续校验租约，防止聊天动作或其派生任务在切换后迟到落地。
    """
    receipt_sender = (
        _send_to_target_receipt
        if current_context_lease() is not None
        else _send_to_target_receipt_permitted
    )
    receipt = await receipt_sender(
        message_segment=message_segment,
        stream_id=stream_id,
        display_message=display_message,
        typing=typing,
        set_reply=set_reply,
        reply_message=reply_message,
        storage_message=storage_message,
        show_log=show_log,
        selected_expressions=selected_expressions,
    )
    # Legacy callers treat intentional policy suppression as handled success.
    return receipt.status in {SendStatus.DELIVERED, SendStatus.SUPPRESSED}


def db_message_to_message_recv(message_obj: "DatabaseMessages") -> MessageRecv:
    """将数据库dict重建为MessageRecv对象
    Args:
        message_dict: 消息字典

    Returns:
        Optional[MessageRecv]: 找到的消息，如果没找到则返回None
    """
    # 构建MessageRecv对象
    user_info = {
        "platform": message_obj.user_info.platform or "",
        "user_id": message_obj.user_info.user_id or "",
        "user_nickname": message_obj.user_info.user_nickname or "",
        "user_cardname": message_obj.user_info.user_cardname or "",
    }

    group_info = {}
    if message_obj.chat_info.group_info:
        group_info = {
            "platform": message_obj.chat_info.group_info.group_platform or "",
            "group_id": message_obj.chat_info.group_info.group_id or "",
            "group_name": message_obj.chat_info.group_info.group_name or "",
        }

    format_info = {"content_format": "", "accept_format": ""}
    template_info = {"template_items": {}}

    message_info = {
        "platform": message_obj.chat_info.platform or "",
        "message_id": message_obj.message_id,
        "time": message_obj.time,
        "group_info": group_info,
        "user_info": user_info,
        "additional_config": message_obj.additional_config,
        "format_info": format_info,
        "template_info": template_info,
    }

    message_dict_recv = {
        "message_info": message_info,
        "raw_message": message_obj.processed_plain_text,
        "processed_plain_text": message_obj.processed_plain_text,
    }

    return MessageRecv(message_dict_recv)


# =============================================================================
# 公共API函数 - 预定义类型的发送函数
# =============================================================================


async def text_to_stream_receipt(
    text: str,
    stream_id: str,
    typing: bool = False,
    set_reply: bool = False,
    reply_message: Optional["DatabaseMessages"] = None,
    storage_message: bool = True,
    selected_expressions: Optional[List[int]] = None,
    display_message: str = "",
) -> SendReceipt:
    """发送文本并返回可用于 Focus handoff ACK 的真实投递回执。"""
    return await _send_to_target_receipt(
        message_segment=Seg(type="text", data=text),
        stream_id=stream_id,
        display_message=display_message,
        typing=typing,
        set_reply=set_reply,
        reply_message=reply_message,
        storage_message=storage_message,
        selected_expressions=selected_expressions,
    )


async def text_to_stream(
    text: str,
    stream_id: str,
    typing: bool = False,
    set_reply: bool = False,
    reply_message: Optional["DatabaseMessages"] = None,
    storage_message: bool = True,
    selected_expressions: Optional[List[int]] = None,
) -> bool:
    """向指定流发送文本消息

    Args:
        text: 要发送的文本内容
        stream_id: 聊天流ID
        typing: 是否显示正在输入
        reply_to: 回复消息，格式为"发送者:消息内容"
        storage_message: 是否存储消息到数据库

    Returns:
        bool: 是否发送成功
    """
    return await _send_to_target(
        message_segment=Seg(type="text", data=text),
        stream_id=stream_id,
        display_message="",
        typing=typing,
        set_reply=set_reply,
        reply_message=reply_message,
        storage_message=storage_message,
        selected_expressions=selected_expressions,
    )


async def emoji_to_stream(
    emoji_base64: str,
    stream_id: str,
    storage_message: bool = True,
    set_reply: bool = False,
    reply_message: Optional["DatabaseMessages"] = None,
) -> bool:
    """向指定流发送表情包

    Args:
        emoji_base64: 表情包的base64编码
        stream_id: 聊天流ID
        storage_message: 是否存储消息到数据库

    Returns:
        bool: 是否发送成功
    """
    return await _send_to_target(
        message_segment=Seg(type="emoji", data=emoji_base64),
        stream_id=stream_id,
        display_message="",
        typing=False,
        storage_message=storage_message,
        set_reply=set_reply,
        reply_message=reply_message,
    )


async def image_to_stream(
    image_base64: str,
    stream_id: str,
    storage_message: bool = True,
    set_reply: bool = False,
    reply_message: Optional["DatabaseMessages"] = None,
) -> bool:
    """向指定流发送图片

    Args:
        image_base64: 图片的base64编码
        stream_id: 聊天流ID
        storage_message: 是否存储消息到数据库

    Returns:
        bool: 是否发送成功
    """
    return await _send_to_target(
        message_segment=Seg(type="image", data=image_base64),
        stream_id=stream_id,
        display_message="",
        typing=False,
        storage_message=storage_message,
        set_reply=set_reply,
        reply_message=reply_message,
    )


async def command_to_stream(
    command: Union[str, dict],
    stream_id: str,
    storage_message: bool = True,
    display_message: str = "",
) -> bool:
    """向指定流发送命令

    Args:
        command: 命令
        stream_id: 聊天流ID
        storage_message: 是否存储消息到数据库
        display_message: 显示消息

    Returns:
        bool: 是否发送成功
    """
    return await _send_to_target(
        message_segment=Seg(type="command", data=command),  # type: ignore
        stream_id=stream_id,
        display_message=display_message,
        typing=False,
        storage_message=storage_message,
        set_reply=False,
    )


async def custom_to_stream(
    message_type: str,
    content: str | Dict,
    stream_id: str,
    display_message: str = "",
    typing: bool = False,
    reply_message: Optional["DatabaseMessages"] = None,
    set_reply: bool = False,
    storage_message: bool = True,
    show_log: bool = True,
) -> bool:
    """向指定流发送自定义类型消息

    Args:
        message_type: 消息类型，如"text"、"image"、"emoji"、"video"、"file"等
        content: 消息内容（通常是base64编码或文本）
        stream_id: 聊天流ID
        display_message: 显示消息
        typing: 是否显示正在输入
        reply_to: 回复消息，格式为"发送者:消息内容"
        storage_message: 是否存储消息到数据库
        show_log: 是否显示日志
    Returns:
        bool: 是否发送成功
    """
    return await _send_to_target(
        message_segment=Seg(type=message_type, data=content),  # type: ignore
        stream_id=stream_id,
        display_message=display_message,
        typing=typing,
        reply_message=reply_message,
        set_reply=set_reply,
        storage_message=storage_message,
        show_log=show_log,
    )


async def custom_reply_set_to_stream(
    reply_set: "ReplySetModel",
    stream_id: str,
    display_message: str = "",  # 基本没用
    typing: bool = False,
    reply_message: Optional["DatabaseMessages"] = None,
    set_reply: bool = False,
    storage_message: bool = True,
    show_log: bool = True,
) -> bool:
    """
    向指定流发送混合型消息集

    Args:
        reply_set: ReplySetModel 对象，包含多个 ReplyContent
        stream_id: 聊天流ID
        display_message: 显示消息
        typing: 是否显示正在输入
        reply_to: 回复消息，格式为"发送者:消息内容"
        storage_message: 是否存储消息到数据库
        show_log: 是否显示日志
    """
    if _should_suppress_reply_set(reply_set):
        texts = []
        for rc in reply_set.reply_data:
            if rc.content_type == ReplyContentType.TEXT:
                texts.append(str(rc.content))
            elif rc.content_type == ReplyContentType.HYBRID:
                if isinstance(rc.content, list):
                    for sub in rc.content:
                        if sub.content_type == ReplyContentType.TEXT:
                            texts.append(str(sub.content))
        pre_filtered = " ".join(texts)
        logger.warning(f"[SendAPI] 过滤前信息: {pre_filtered}")
        logger.error("[SendAPI] 检测到可疑回复模板，已替换为 Filtered")
        return await text_to_stream(
            text="Filtered",
            stream_id=stream_id,
            typing=False,
            set_reply=set_reply,
            reply_message=reply_message,
            storage_message=storage_message,
        )

    flag: bool = True
    for reply_content in reply_set.reply_data:
        status: bool = False
        message_seg, need_typing = _parse_content_to_seg(reply_content)
        status = await _send_to_target(
            message_segment=message_seg,
            stream_id=stream_id,
            display_message=display_message,
            typing=bool(need_typing and typing),
            reply_message=reply_message,
            set_reply=set_reply,
            storage_message=storage_message,
            show_log=show_log,
        )
        if not status:
            flag = False
            logger.error(
                f"[SendAPI] 发送{repr(reply_content.content_type)}消息失败，消息内容：{str(reply_content.content)[:100]}"
            )

    return flag


def _parse_content_to_seg(reply_content: "ReplyContent") -> Tuple[Seg, bool]:
    """
    把 ReplyContent 转换为 Seg 结构 (Forward 中仅递归一次)
    Args:
        reply_content: ReplyContent 对象
    Returns:
        Tuple[Seg, bool]: 转换后的 Seg 结构和是否需要typing的标志
    """
    content_type = reply_content.content_type
    if content_type == ReplyContentType.TEXT:
        text_data: str = reply_content.content  # type: ignore
        return Seg(type="text", data=text_data), True
    elif content_type == ReplyContentType.IMAGE:
        return Seg(type="image", data=reply_content.content), False  # type: ignore
    elif content_type == ReplyContentType.EMOJI:
        return Seg(type="emoji", data=reply_content.content), False  # type: ignore
    elif content_type == ReplyContentType.COMMAND:
        return Seg(type="command", data=reply_content.content), False  # type: ignore
    elif content_type == ReplyContentType.VOICE:
        return Seg(type="voice", data=reply_content.content), False  # type: ignore
    elif content_type == ReplyContentType.HYBRID:
        hybrid_message_list_data: List[ReplyContent] = reply_content.content  # type: ignore
        assert isinstance(hybrid_message_list_data, list), "混合类型内容必须是列表"
        sub_seg_list: List[Seg] = []
        for sub_content in hybrid_message_list_data:
            sub_content_type = sub_content.content_type
            sub_content_data = sub_content.content

            if sub_content_type == ReplyContentType.TEXT:
                sub_seg_list.append(Seg(type="text", data=sub_content_data))  # type: ignore
            elif sub_content_type == ReplyContentType.IMAGE:
                sub_seg_list.append(Seg(type="image", data=sub_content_data))  # type: ignore
            elif sub_content_type == ReplyContentType.EMOJI:
                sub_seg_list.append(Seg(type="emoji", data=sub_content_data))  # type: ignore
            else:
                logger.warning(f"[SendAPI] 混合类型中不支持的子内容类型: {repr(sub_content_type)}")
                continue
        return Seg(type="seglist", data=sub_seg_list), True
    elif content_type == ReplyContentType.FORWARD:
        forward_message_list_data: List["ForwardNode"] = reply_content.content  # type: ignore
        assert isinstance(forward_message_list_data, list), "转发类型内容必须是列表"
        forward_message_list: List[Dict] = []
        for forward_node in forward_message_list_data:
            message_segment = Seg(type="id", data=forward_node.content)  # type: ignore
            user_info: Optional[UserInfo] = None
            if forward_node.user_id and forward_node.user_nickname:
                assert isinstance(forward_node.content, list), "转发节点内容必须是列表"
                user_info = UserInfo(user_id=forward_node.user_id, user_nickname=forward_node.user_nickname)
                single_node_content: List[Seg] = []
                for sub_content in forward_node.content:
                    if sub_content.content_type != ReplyContentType.FORWARD:
                        sub_seg, _ = _parse_content_to_seg(sub_content)
                        single_node_content.append(sub_seg)
                message_segment = Seg(type="seglist", data=single_node_content)
            forward_message_list.append(
                MessageBase(
                    message_segment=message_segment, message_info=BaseMessageInfo(user_info=user_info)
                ).to_dict()
            )
        return Seg(type="forward", data=forward_message_list), False  # type: ignore
    else:
        message_type_in_str = content_type.value if isinstance(content_type, ReplyContentType) else str(content_type)
        return Seg(type=message_type_in_str, data=reply_content.content), True  # type: ignore


def should_filter_text(text: str) -> bool:
    """公共接口：检测文本是否命中 response_filter 规则。
    插件在发布到外部平台（如 QQ 空间）前应调用此函数检查内容。
    Returns True 表示内容应被拦截。
    """
    return _should_suppress_text_reply(text)
