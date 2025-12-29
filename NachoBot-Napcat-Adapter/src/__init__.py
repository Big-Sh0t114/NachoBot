from enum import Enum
import tomlkit
import os
from .logger import logger


class CommandType(Enum):
    """命令类型"""

    GROUP_BAN = "set_group_ban"  # 禁言用户
    GROUP_WHOLE_BAN = "set_group_whole_ban"  # 群全体禁言
    GROUP_KICK = "set_group_kick"  # 踢出群聊
    SEND_POKE = "send_poke"  # 戳一戳
    DELETE_MSG = "delete_msg"  # 撤回消息
    AI_VOICE_SEND = "send_group_ai_record"  # 发送群AI语音
    MESSAGE_LIKE = "message_like"  # 给消息贴表情

    def __str__(self) -> str:
        return self.value


pyproject_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pyproject.toml")
toml_data = tomlkit.parse(open(pyproject_path, "r", encoding="utf-8").read())
version = toml_data["project"]["version"]
logger.info(f"版本\n\nMaiBot-Napcat-Adapter 版本: {version}\n喜欢的话点个star喵~\n")
