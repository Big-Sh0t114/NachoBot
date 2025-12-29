from dataclasses import dataclass, field
from typing import Literal

from src.config.config_base import ConfigBase

"""
须知：
1. 本文件中记录了所有的配置项
2. 所有新增的class都需要继承自ConfigBase
3. 所有新增的class都应在config.py中的Config类中添加字段
4. 对于新增的字段，若为可选项，则应在其后添加field()并设置default_factory或default
"""

ADAPTER_PLATFORM = "qq"


@dataclass
class NicknameConfig(ConfigBase):
    nickname: str
    """机器人昵称"""


@dataclass
class NapcatServerConfig(ConfigBase):
    host: str = "localhost"
    """Napcat服务端的主机地址"""

    port: int = 8095
    """Napcat服务端的端口号"""

    token: str = ""
    """Napcat服务端的访问令牌，若无则留空"""

    heartbeat_interval: int = 30
    """Napcat心跳间隔时间，单位为秒"""


@dataclass
class MaiBotServerConfig(ConfigBase):
    platform_name: str = field(default=ADAPTER_PLATFORM, init=False)
    """平台名称，“qq”"""

    host: str = "localhost"
    """MaiMCore的主机地址"""

    port: int = 8000
    """MaiMCore的端口号"""


@dataclass
class ChatConfig(ConfigBase):
    group_list_type: Literal["whitelist", "blacklist"] = "whitelist"
    """群聊列表类型 白名单/黑名单"""

    group_list: list[int] = field(default_factory=[])
    """群聊列表"""

    private_list_type: Literal["whitelist", "blacklist"] = "whitelist"
    """私聊列表类型 白名单/黑名单"""

    private_list: list[int] = field(default_factory=[])
    """私聊列表"""

    ban_user_id: list[int] = field(default_factory=[])
    """被封禁的用户ID列表，封禁后将无法与其进行交互"""

    ban_qq_bot: bool = False
    """是否屏蔽QQ官方机器人，若为True，则所有QQ官方机器人将无法与MaiMCore进行交互"""

    enable_poke: bool = True
    """是否启用戳一戳功能"""


@dataclass
class VoiceConfig(ConfigBase):
    use_tts: bool = False
    """是否启用TTS功能"""


@dataclass
class DebugConfig(ConfigBase):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    """日志级别，默认为INFO"""
