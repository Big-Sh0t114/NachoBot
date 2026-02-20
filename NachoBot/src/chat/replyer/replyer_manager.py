from typing import Dict, Optional

from src.common.logger import get_logger
from src.chat.message_receive.chat_stream import ChatStream, get_chat_manager
from src.chat.replyer.group_generator import DefaultReplyer, AdvancedGroupReplyer
from src.chat.replyer.private_generator import PrivateReplyer
from src.chat.replyer.advanced_generator import AdvancedPrivateReplyer
from src.chat.advanced.advanced_manager import advanced_manager

logger = get_logger("ReplyerManager")


class ReplyerManager:
    def __init__(self):
        self._repliers: Dict[str, DefaultReplyer | PrivateReplyer] = {}

    def get_replyer(
        self,
        chat_stream: Optional[ChatStream] = None,
        chat_id: Optional[str] = None,
        request_type: str = "replyer",
    ) -> Optional[DefaultReplyer | PrivateReplyer]:
        """
        获取或创建回复器实例。

        model_configs 仅在首次为某个 chat_id/stream_id 创建实例时有效。
        后续调用将返回已缓存的实例，忽略 model_configs 参数。
        """
        stream_id = chat_stream.stream_id if chat_stream else chat_id
        if not stream_id:
            logger.warning("[ReplyerManager] 缺少 stream_id，无法获取回复器。")
            return None

        target_stream = chat_stream
        if not target_stream:
            if chat_manager := get_chat_manager():
                target_stream = chat_manager.get_stream(stream_id)

        if not target_stream:
            logger.warning(f"[ReplyerManager] 未找到 stream_id='{stream_id}' 的聊天流，无法创建回复器。")
            return None

        # 根据是否开启高级模式选择生成器类型
        advanced_on = advanced_manager.is_on(target_stream) if target_stream else False
        if target_stream.group_info:
            desired_cls = AdvancedGroupReplyer if advanced_on else DefaultReplyer
        else:
            desired_cls = AdvancedPrivateReplyer if advanced_on else PrivateReplyer

        # 修改缓存键，包含 request_type，防止 file_edit 被普通 replyer 覆盖
        cache_key = f"{stream_id}_{request_type}"

        # 如果已有缓存但类型不匹配，替换；否则沿用缓存
        cached = self._repliers.get(cache_key)
        if cached:
            cached_cls = type(cached)
            if cached_cls is desired_cls:
                logger.debug(f"[ReplyerManager] 为 cache_key '{cache_key}' 返回已存在的回复器实例。")
                return cached
            logger.debug(
                f"[ReplyerManager] cache_key '{cache_key}' 回复器类型切换 {cached_cls.__name__} -> {desired_cls.__name__}，重新创建。"
            )

        logger.debug(f"[ReplyerManager] 为 cache_key '{cache_key}' 创建新的回复器实例并缓存。")
        replyer = desired_cls(
            chat_stream=target_stream,
            request_type=request_type,
        )
        self._repliers[cache_key] = replyer
        return replyer


# 创建一个全局实例
replyer_manager = ReplyerManager()
