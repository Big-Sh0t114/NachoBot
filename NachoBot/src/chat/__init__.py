"""
MaiBot模块系统
包含聊天、情绪、记忆、日程等功能模块
"""

from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.emoji_system.emoji_manager import get_emoji_manager

# 导出主要组件供外部使用
__all__ = [
    "get_chat_manager",
    "get_emoji_manager",
]
