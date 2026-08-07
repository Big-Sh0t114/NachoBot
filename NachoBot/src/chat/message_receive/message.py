import re
import time
import urllib3

from abc import abstractmethod
from dataclasses import dataclass
from rich.traceback import install
from typing import Optional, Any, List
from ncnk_message import Seg, UserInfo, BaseMessageInfo, MessageBase

import os
from pathlib import Path

from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.utils.utils_image import get_image_manager
from src.chat.utils.utils_voice import get_voice_text
from src.chat.sandbox.sandbox_manager import sandbox_manager
from .chat_stream import ChatStream

install(extra_lines=3)

logger = get_logger("chat_message")

# 禁用SSL警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def _extract_urls_from_unknown_data(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        raw_urls = _URL_PATTERN.findall(data)
    else:
        raw_urls = _URL_PATTERN.findall(str(data))

    if not raw_urls:
        return ""

    cleaned = []
    seen = set()
    for url in raw_urls:
        trimmed = url.rstrip(").,;\"'")
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        cleaned.append(trimmed)
    return " ".join(cleaned)


# 这个类是消息数据类，用于存储和管理消息数据。
# 它定义了消息的属性，包括群组ID、用户ID、消息ID、原始消息内容、纯文本内容和时间戳。
# 它还定义了两个辅助属性：keywords用于提取消息的关键词，is_plain_text用于判断消息是否为纯文本。


@dataclass
class Message(MessageBase):
    chat_stream: "ChatStream" = None  # type: ignore
    reply: Optional["Message"] = None
    processed_plain_text: str = ""

    def __init__(
        self,
        message_id: str,
        chat_stream: "ChatStream",
        user_info: UserInfo,
        message_segment: Optional[Seg] = None,
        timestamp: Optional[float] = None,
        reply: Optional["MessageRecv"] = None,
        processed_plain_text: str = "",
    ):
        # 使用传入的时间戳或当前时间
        current_timestamp = timestamp if timestamp is not None else round(time.time(), 3)
        # 构造基础消息信息
        message_info = BaseMessageInfo(
            platform=chat_stream.platform,
            message_id=message_id,
            time=current_timestamp,
            group_info=chat_stream.group_info,
            user_info=user_info,
        )

        # 调用父类初始化
        super().__init__(message_info=message_info, message_segment=message_segment, raw_message=None)  # type: ignore

        self.chat_stream = chat_stream
        # 文本处理相关属性
        self.processed_plain_text = processed_plain_text

        # 回复消息
        self.reply = reply

    async def _process_message_segments(self, segment: Seg) -> str:
        # sourcery skip: remove-unnecessary-else, swap-if-else-branches
        """递归处理消息段，转换为文字描述

        Args:
            segment: 要处理的消息段

        Returns:
            str: 处理后的文本
        """
        if segment.type == "seglist":
            # 处理消息段列表
            segments_text = []
            for seg in segment.data:
                processed = await self._process_message_segments(seg)  # type: ignore
                if processed:
                    segments_text.append(processed)
            return " ".join(segments_text)
        elif segment.type == "forward":
            segments_text = []
            for node_dict in segment.data:
                message = MessageBase.from_dict(node_dict)  # type: ignore
                processed_text = await self._process_message_segments(message.message_segment)
                if processed_text:
                    segments_text.append(f"{global_config.bot.nickname}: {processed_text}")
            return "[合并消息]: " + "\n--  ".join(segments_text)
        else:
            # 处理单个消息段
            return await self._process_single_segment(segment)  # type: ignore

    @abstractmethod
    async def _process_single_segment(self, segment) -> str:
        pass


@dataclass
class MessageRecv(Message):
    """接收消息类，用于处理从MessageCQ序列化的消息"""

    def __init__(self, message_dict: dict[str, Any]):
        """从MessageCQ的字典初始化

        Args:
            message_dict: MessageCQ序列化后的字典
        """
        self.message_info = BaseMessageInfo.from_dict(message_dict.get("message_info", {}))
        self.message_segment = Seg.from_dict(message_dict.get("message_segment", {}))
        self.raw_message = message_dict.get("raw_message")
        self.processed_plain_text = message_dict.get("processed_plain_text", "")
        self.is_emoji = False
        self.has_emoji = False
        self.is_picid = False
        self.has_picid = False
        self.is_voice = False
        self.is_video = False
        self.is_mentioned = None
        self.is_at = False
        self.reply_probability_boost = 0.0
        self.is_notify = False

        self.is_command = False
        self.force_command = False

        self.priority_mode = "interest"
        self.priority_info = None
        self.interest_value: float = None  # type: ignore

        self.key_words = []
        self.key_words_lite = []

    def update_chat_stream(self, chat_stream: "ChatStream"):
        self.chat_stream = chat_stream

    async def process(self) -> None:
        """处理消息内容，生成纯文本和详细文本

        这个方法必须在创建实例后显式调用，因为它包含异步操作。
        """
        self.processed_plain_text = await self._process_message_segments(self.message_segment)

    async def _save_file_to_sandbox(self, file_data: str, filename: str) -> str:
        """Save file data to sandbox and return local path
        Args:
            file_data: Can be a URL, base64 string, or local path (if from adapter).
                       For now, we assume it might be a URL/Path from the adapter.
            filename: The name of the file
        """
        if not self.chat_stream:
            return ""

        try:
            import httpx

            sandbox = sandbox_manager.get_sandbox(self.chat_stream.stream_id)

            # Case 1: URL
            if file_data.startswith("http"):
                async with httpx.AsyncClient() as client:
                    # Check size first using HEAD
                    head_resp = await client.head(file_data)
                    if head_resp.status_code == 200:
                        content_length = int(head_resp.headers.get("Content-Length", 0))
                        if content_length > 1048576:  # 1MB
                            raise ValueError(f"File exceeds 1MB limit ({content_length} bytes)")

                    resp = await client.get(file_data)
                    if resp.status_code == 200:
                        # Fallback size check just in case HEAD didn't give Content-Length
                        if len(resp.content) > 1048576:
                            raise ValueError(f"File exceeds 1MB limit ({len(resp.content)} bytes)")
                        return sandbox.save_file(resp.content, filename)

            # Case 2: Local Path (already on disk, e.g. from OneBot/NapCat)
            # If the adapter saves it somewhere, we might just copy it or leave it.
            # But sandbox implies isolation. Let's copy it.
            elif os.path.exists(file_data):
                if os.path.getsize(file_data) > 1048576:  # 1MB limit
                    raise ValueError(f"Local file exceeds 1MB limit ({os.path.getsize(file_data)} bytes)")

                with open(file_data, "rb") as f:
                    content = f.read()
                return sandbox.save_file(content, filename)

            # Case 3: Base64 (Legacy/Other) - To be implemented if needed

        except ValueError as ve:
            # Re-raise ValueError so _process_single_segment can catch it specifically for size limits
            raise ve
        except Exception as e:
            logger.error(f"Failed to save file to sandbox: {e}")

        return ""

    async def _process_single_segment(self, segment: Seg) -> str:
        """处理单个消息段

        Args:
            segment: 消息段

        Returns:
            str: 处理后的文本
        """
        try:
            if segment.type == "file":
                # Data format expected: {"url": "...", "name": "...", "size": ...} or just url string?
                # Adapters usually give a dict for file.

                file_name = "unknown_file"
                file_url = ""

                if isinstance(segment.data, dict):
                    file_name = segment.data.get("name", f"file_{int(time.time())}")
                    file_url = segment.data.get("url", "")
                    # Some adapters might use 'path' or 'file'
                    if not file_url:
                        file_url = segment.data.get("path") or segment.data.get("file")
                elif isinstance(segment.data, str):
                    # If it's a string, assume it's url/path
                    file_url = segment.data
                    file_name = Path(file_url).name

                # Check whitelist first
                user_id = ""
                if hasattr(self, "message_info") and self.message_info:
                    if self.message_info.sender_info:
                        user_id = str(self.message_info.sender_info.user_id)
                    elif self.message_info.user_info:
                        user_id = str(self.message_info.user_info.user_id)

                if not user_id or user_id not in global_config.bot.sandbox_whitelist:
                    logger.warning(f"用户 {user_id} 不在沙盒白名单中，拒绝自动保存文件: {file_name}")
                    return f"[接收到文件: {file_name}，但发送者不在沙盒白名单，已忽略自动保存]"

                if file_url:
                    try:
                        saved_path = await self._save_file_to_sandbox(str(file_url), str(file_name))
                        if saved_path:
                            # Store in a new attribute 'files' if we want to track it
                            if not hasattr(self, "files"):
                                self.files = []
                            self.files.append(saved_path)

                            # Auto-preview small text files (< 3KB)
                            preview_text = ""
                            try:
                                if os.path.exists(saved_path) and os.path.getsize(saved_path) < 3072:
                                    with open(saved_path, "r", encoding="utf-8") as f:
                                        content = f.read()
                                        if content:
                                            preview_text = f"\n[自动预览]:\n{content}"
                            except Exception:
                                pass  # Not a text file or read error

                            return f"[文件: {file_name} (已保存到沙盒)]{preview_text}"
                        else:
                            return f"[文件: {file_name} (下载失败)]"
                    except ValueError as ve:
                        logger.warning(f"用户 {user_id} 上传的文件 {file_name} 超过大小限制: {ve}")
                        return f"[接收到文件: {file_name}，但文件大小超过1MB限制，已丢弃]"
                return f"[文件: {file_name}]"

            elif segment.type == "screen":
                # Legacy screen events are ignored by the regular HeartFlow message stream.
                return ""
            elif segment.type == "text":
                self.is_picid = False
                self.is_emoji = False
                text_data = str(segment.data)
                if text_data.strip().startswith("<SCREEN_INFO>"):
                    # Legacy screen payloads remain intentionally silent.
                    return ""
                return text_data
            elif segment.type == "image":
                # 如果是base64图片数据
                if isinstance(segment.data, str):
                    self.has_picid = True
                    self.is_picid = True
                    self.is_emoji = False
                    image_manager = get_image_manager()
                    # print(f"segment.data: {segment.data}")
                    _, processed_text = await image_manager.process_image(
                        segment.data,
                        additional_config=self.message_info.additional_config,
                    )
                    return processed_text
                return "[发了一张图片，网卡了加载不出来]"
            elif segment.type == "emoji":
                self.has_emoji = True
                self.is_emoji = True
                self.is_picid = False
                self.is_voice = False
                if isinstance(segment.data, str):
                    return await get_image_manager().get_emoji_description(
                        segment.data,
                        additional_config=self.message_info.additional_config,
                    )
                return "[发了一个表情包，网卡了加载不出来]"
            elif segment.type == "voice":
                self.is_picid = False
                self.is_emoji = False
                self.is_voice = True
                self.is_video = False
                if isinstance(segment.data, str):
                    return await get_voice_text(segment.data)
                return "[发了一段语音，网卡了加载不出来]"
            elif segment.type == "video":
                self.is_picid = False
                self.is_emoji = False
                self.is_voice = False
                self.is_video = True
                try:
                    from src.chat.utils.utils_video import get_video_manager

                    manager = get_video_manager()
                    return await manager.process_video(
                        segment.data,  # type: ignore
                        additional_config=self.message_info.additional_config,
                    )
                except Exception as e:
                    logger.error(f"处理视频段失败: {e}")
                    return "[视频(接收失败)]"
            elif segment.type == "mention_bot":
                self.is_picid = False
                self.is_emoji = False
                self.is_voice = False
                self.is_mentioned = float(segment.data)  # type: ignore
                return ""
            elif segment.type == "priority_info":
                self.is_picid = False
                self.is_emoji = False
                self.is_voice = False
                if isinstance(segment.data, dict):
                    # 处理优先级信息
                    self.priority_mode = "priority"
                    self.priority_info = segment.data
                    """
                    {
                        'message_type': 'vip', # vip or normal
                        'message_priority': 1.0, # 优先级，大为优先，float
                    }
                    """
                return ""
            else:
                extracted_urls = _extract_urls_from_unknown_data(segment.data)
                if extracted_urls:
                    return extracted_urls
                return ""
        except Exception as e:
            logger.error(f"处理消息段失败: {str(e)}, 类型: {segment.type}, 数据: {segment.data}")
            return f"[处理失败的{segment.type}消息]"


@dataclass
class MessageProcessBase(Message):
    """消息处理基类，用于处理中和发送中的消息"""

    def __init__(
        self,
        message_id: str,
        chat_stream: "ChatStream",
        bot_user_info: UserInfo,
        message_segment: Optional[Seg] = None,
        reply: Optional["MessageRecv"] = None,
        thinking_start_time: float = 0,
        timestamp: Optional[float] = None,
    ):
        # 调用父类初始化，传递时间戳
        super().__init__(
            message_id=message_id,
            timestamp=timestamp,
            chat_stream=chat_stream,
            user_info=bot_user_info,
            message_segment=message_segment,
            reply=reply,
        )

        # 处理状态相关属性
        self.thinking_start_time = thinking_start_time
        self.thinking_time = 0

    def update_thinking_time(self) -> float:
        """更新思考时间"""
        self.thinking_time = round(time.time() - self.thinking_start_time, 2)
        return self.thinking_time

    async def _process_single_segment(self, segment: Seg) -> str:
        """处理单个消息段

        Args:
            segment: 要处理的消息段

        Returns:
            str: 处理后的文本
        """
        try:
            if segment.type == "text":
                return segment.data  # type: ignore
            elif segment.type == "image":
                # 如果是base64图片数据
                if isinstance(segment.data, str):
                    return await get_image_manager().get_image_description(segment.data)
                return "[图片，网卡了加载不出来]"
            elif segment.type == "emoji":
                if isinstance(segment.data, str):
                    return await get_image_manager().get_emoji_tag(segment.data)
                return "[表情，网卡了加载不出来]"
            elif segment.type == "voice":
                if isinstance(segment.data, str):
                    return await get_voice_text(segment.data)
                return "[发了一段语音，网卡了加载不出来]"
            elif segment.type == "video":
                return "[引用了一段视频]"
            elif segment.type == "at":
                return f"[@{segment.data}]"
            elif segment.type == "reply":
                if self.reply and hasattr(self.reply, "processed_plain_text"):
                    # print(f"self.reply.processed_plain_text: {self.reply.processed_plain_text}")
                    # print(f"reply: {self.reply}")
                    return f"[回复<{self.reply.message_info.user_info.user_nickname}:{self.reply.message_info.user_info.user_id}> 的消息：{self.reply.processed_plain_text}]"  # type: ignore
                return ""
            else:
                return f"[{segment.type}:{str(segment.data)}]"
        except Exception as e:
            logger.error(f"处理消息段失败: {str(e)}, 类型: {segment.type}, 数据: {segment.data}")
            return f"[处理失败的{segment.type}消息]"

    def _generate_detailed_text(self) -> str:
        """生成详细文本，包含时间和用户信息"""
        # time_str = time.strftime("%m-%d %H:%M:%S", time.localtime(self.message_info.time))
        timestamp = self.message_info.time
        user_info = self.message_info.user_info

        name = f"<{self.message_info.platform}:{user_info.user_id}:{user_info.user_nickname}:{user_info.user_cardname}>"  # type: ignore
        return f"[{timestamp}]，{name} 说：{self.processed_plain_text}\n"


@dataclass
class MessageSending(MessageProcessBase):
    """发送状态的消息类"""

    def __init__(
        self,
        message_id: str,
        chat_stream: "ChatStream",
        bot_user_info: UserInfo,
        sender_info: UserInfo | None,  # 用来记录发送者信息
        message_segment: Seg,
        display_message: str = "",
        reply: Optional["MessageRecv"] = None,
        is_head: bool = False,
        is_emoji: bool = False,
        thinking_start_time: float = 0,
        apply_set_reply_logic: bool = False,
        reply_to: Optional[str] = None,
        selected_expressions: Optional[List[int]] = None,
    ):
        # 调用父类初始化
        super().__init__(
            message_id=message_id,
            chat_stream=chat_stream,
            bot_user_info=bot_user_info,
            message_segment=message_segment,
            reply=reply,
            thinking_start_time=thinking_start_time,
        )

        # 发送状态特有属性
        self.sender_info = sender_info
        self.reply_to_message_id = reply.message_info.message_id if reply else None
        self.is_head = is_head
        self.is_emoji = is_emoji
        self.apply_set_reply_logic = apply_set_reply_logic

        self.reply_to = reply_to

        # 用于显示发送内容与显示不一致的情况
        self.display_message = display_message

        self.interest_value = 0.0

        self.selected_expressions = selected_expressions

    def build_reply(self):
        """设置回复消息"""
        if self.reply:
            msg_id = self.reply.message_info.message_id
            # 跳过非平台消息（如 notice 戳一戳、send_api 内部消息），这些没有有效的平台消息 ID
            if isinstance(msg_id, str) and (msg_id.startswith("notice") or msg_id.startswith("send_api_")):
                logger.debug(f"跳过引用回复：message_id '{msg_id}' 不是有效的平台消息ID")
                return
            self.reply_to_message_id = msg_id
            self.message_segment = Seg(
                type="seglist",
                data=[
                    Seg(type="reply", data=msg_id),  # type: ignore
                    self.message_segment,
                ],
            )

    async def process(self) -> None:
        """处理消息内容，生成纯文本和详细文本"""
        if self.message_segment:
            self.processed_plain_text = await self._process_message_segments(self.message_segment)

    def to_dict(self):
        ret = super().to_dict()
        ret["message_info"]["user_info"] = (
            self.chat_stream.user_info.to_dict() if self.chat_stream and self.chat_stream.user_info else None
        )

        # 保留本条回复所对应的原始平台消息 ID。WebUI Chat 依靠该字段
        # 将异步、多段回复精确关联到触发它的那一轮请求，避免迟到回复
        # 被下一轮请求错误领取。
        if self.reply_to_message_id:
            additional_config = ret["message_info"].get("additional_config")
            if not isinstance(additional_config, dict):
                additional_config = {}
                ret["message_info"]["additional_config"] = additional_config
            additional_config["reply_to_message_id"] = self.reply_to_message_id

        return ret

    def is_private_message(self) -> bool:
        """判断是否为私聊消息"""
        return self.message_info.group_info is None or self.message_info.group_info.group_id is None


@dataclass
class MessageSet:
    """消息集合类，可以存储多个发送消息"""

    def __init__(self, chat_stream: "ChatStream", message_id: str):
        self.chat_stream = chat_stream
        self.message_id = message_id
        self.messages: list[MessageSending] = []
        self.time = round(time.time(), 3)  # 保留3位小数

    def add_message(self, message: MessageSending) -> None:
        """添加消息到集合"""
        if not isinstance(message, MessageSending):
            raise TypeError("MessageSet只能添加MessageSending类型的消息")
        self.messages.append(message)
        self.messages.sort(key=lambda x: x.message_info.time)  # type: ignore

    def get_message_by_index(self, index: int) -> Optional[MessageSending]:
        """通过索引获取消息"""
        return self.messages[index] if 0 <= index < len(self.messages) else None

    def get_message_by_time(self, target_time: float) -> Optional[MessageSending]:
        """获取最接近指定时间的消息"""
        if not self.messages:
            return None

        left, right = 0, len(self.messages) - 1
        while left < right:
            mid = (left + right) // 2
            if self.messages[mid].message_info.time < target_time:  # type: ignore
                left = mid + 1
            else:
                right = mid

        return self.messages[left]

    def clear_messages(self) -> None:
        """清空所有消息"""
        self.messages.clear()

    def remove_message(self, message: MessageSending) -> bool:
        """移除指定消息"""
        if message in self.messages:
            self.messages.remove(message)
            return True
        return False

    def __str__(self) -> str:
        return f"MessageSet(id={self.message_id}, count={len(self.messages)})"

    def __len__(self) -> int:
        return len(self.messages)


def message_recv_from_dict(message_dict: dict) -> MessageRecv:
    return MessageRecv(message_dict)


def message_from_db_dict(db_dict: dict) -> MessageRecv:
    """从数据库字典创建MessageRecv实例"""
    # 转换扁平的数据库字典为嵌套结构
    message_info_dict = {
        "platform": db_dict.get("chat_info_platform"),
        "message_id": db_dict.get("message_id"),
        "time": db_dict.get("time"),
        "group_info": {
            "platform": db_dict.get("chat_info_group_platform"),
            "group_id": db_dict.get("chat_info_group_id"),
            "group_name": db_dict.get("chat_info_group_name"),
        },
        "user_info": {
            "platform": db_dict.get("user_platform"),
            "user_id": db_dict.get("user_id"),
            "user_nickname": db_dict.get("user_nickname"),
            "user_cardname": db_dict.get("user_cardname"),
        },
    }

    processed_text = db_dict.get("processed_plain_text", "")

    # 构建 MessageRecv 需要的字典
    recv_dict = {
        "message_info": message_info_dict,
        "message_segment": {"type": "text", "data": processed_text},  # 从纯文本重建消息段
        "raw_message": None,  # 数据库中未存储原始消息
        "processed_plain_text": processed_text,
    }

    # 创建 MessageRecv 实例
    msg = MessageRecv(recv_dict)

    # 从数据库字典中填充其他可选字段
    msg.interest_value = db_dict.get("interest_value", 0.0)
    msg.is_mentioned = db_dict.get("is_mentioned")
    msg.priority_mode = db_dict.get("priority_mode", "interest")
    msg.priority_info = db_dict.get("priority_info")
    msg.is_emoji = db_dict.get("is_emoji", False)
    msg.is_picid = db_dict.get("is_picid", False)

    return msg
