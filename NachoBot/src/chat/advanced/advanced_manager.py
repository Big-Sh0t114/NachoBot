from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from src.common.logger import get_logger
from src.config.config import global_config

if TYPE_CHECKING:
    from src.chat.message_receive.chat_stream import ChatStream


logger = get_logger("advanced_manager")


class AdvancedManager:
    """管理高级模式的白名单和开关状态。"""

    def __init__(self) -> None:
        self._states_user: dict[str, bool] = {}
        self._states_stream: dict[str, bool] = {}
        # 兼容旧字段名，避免外部引用报错
        self._states = self._states_user

    def is_allowed(self, user_id: Optional[str]) -> bool:
        if not user_id:
            return False
        uid = str(user_id)
        wl = set(global_config.advanced.whitelist or [])
        admins = set(getattr(global_config.advanced, "admins", []) or [])
        return uid in wl or uid in admins

    def is_admin(self, user_id: Optional[str]) -> bool:
        if not user_id:
            return False
        return str(user_id) in set(getattr(global_config.advanced, "admins", []) or [])

    def is_on(self, chat_stream: Optional["ChatStream"]) -> bool:
        """仅在私聊且白名单用户时才可能为True。"""
        if not chat_stream or getattr(chat_stream, "group_info", None):
            return False

        user_info = getattr(chat_stream, "user_info", None)
        user_id = getattr(user_info, "user_id", None) if user_info else None
        if not self.is_allowed(user_id):
            return False

        key = str(user_id)
        if key in self._states_user:
            return self._states_user[key]

        # 如果未命中用户级别，尝试按 stream_id 存储的开关（用于兜底）
        stream_key = f"stream:{chat_stream.stream_id}"
        if stream_key in self._states_stream:
            return self._states_stream[stream_key]

        return bool(global_config.advanced.default_enabled)

    def set_state(self, user_id: str, enabled: bool, stream_id: Optional[str] = None) -> bool:
        """设置开关，需调用方已校验白名单。"""
        key = str(user_id)
        self._states_user[key] = enabled
        self._states[key] = enabled  # 兼容旧引用
        if stream_id:
            self._states_stream[f"stream:{stream_id}"] = enabled
        logger.info(f"[Advanced] user={key}, stream={stream_id} set to {enabled}")
        return enabled

    def should_block_tools(self, chat_stream: Optional["ChatStream"]) -> bool:
        return self.is_on(chat_stream) and bool(global_config.advanced.block_tools_when_on)

    def should_block_tts(self, chat_stream: Optional["ChatStream"]) -> bool:
        return self.is_on(chat_stream) and bool(global_config.advanced.block_tts_when_on)

    def list_enabled_users(self) -> list[str]:
        """
        返回当前开启高级模式的用户ID列表（仅限白名单/管理员）。
        规则：显式开关优先，否则取 default_enabled。
        """
        wl = set(global_config.advanced.whitelist or [])
        admins = set(getattr(global_config.advanced, "admins", []) or [])
        candidates = wl | admins
        enabled: list[str] = []
        for uid in candidates:
            if uid in self._states_user:
                if self._states_user[uid]:
                    enabled.append(uid)
            elif global_config.advanced.default_enabled:
                enabled.append(uid)
        return enabled


advanced_manager = AdvancedManager()
