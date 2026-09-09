from dataclasses import dataclass, field
from typing import Any, Dict, Literal

from src.config.config_base import ConfigBase

"""
须知：
1. 本文件中记录了所有的配置项
2. 所有新增的class都需要继承自ConfigBase
3. 所有新增的class都应在config.py中的Config类中添加字段
4. 对于新增的字段，若为可选项，则应在其后添加field()并设置default_factory或default
"""

ADAPTER_PLATFORM = "qq"
QQ_CORE_VISUAL_PROFILE = "qq-core-v1"


@dataclass
class VisualTaskConfig(ConfigBase):
    temperature: float = 0.2
    max_tokens: int = 220
    extra_params: Dict[str, Any] = field(default_factory=dict)

    def to_message_policy(self) -> Dict[str, Any]:
        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "extra_params": dict(self.extra_params),
        }


@dataclass
class VisualConfig(ConfigBase):
    image: VisualTaskConfig = field(
        default_factory=lambda: VisualTaskConfig(
            temperature=0.1,
            max_tokens=220,
            extra_params={"enable_thinking": False},
        )
    )
    emoji: VisualTaskConfig = field(
        default_factory=lambda: VisualTaskConfig(
            temperature=0.2,
            max_tokens=180,
            extra_params={"enable_thinking": False},
        )
    )
    video: VisualTaskConfig = field(
        default_factory=lambda: VisualTaskConfig(
            temperature=0.1,
            max_tokens=280,
            extra_params={"enable_thinking": False},
        )
    )

    def to_message_policy(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "profile": QQ_CORE_VISUAL_PROFILE,
            "image": self.image.to_message_policy(),
            "emoji": self.emoji.to_message_policy(),
            "video": self.video.to_message_policy(),
        }


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
class NachobotServerConfig(ConfigBase):
    platform_name: str = field(default=ADAPTER_PLATFORM, init=False)
    """平台名称，“qq”"""

    host: str = "localhost"
    """Multimodal Adapter 消息中继的主机地址"""

    port: int = 8070
    """消息中继端口，默认 8070，可按部署需要修改"""


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
    """是否屏蔽QQ官方机器人，若为True，则所有QQ官方机器人将无法与NachoCore进行交互"""

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
