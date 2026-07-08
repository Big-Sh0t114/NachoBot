from typing import List, Tuple, Type

# 导入插件系统
from src.plugin_system import BasePlugin, register_plugin, ComponentInfo
from src.plugin_system.base.config_types import ConfigField

# 导入组件
from .messenger import MessengerRelayAction, ConveyCommand

from src.common.logger import get_logger

logger = get_logger("messenger_plugin")


@register_plugin
class MessengerPlugin(BasePlugin):
    """信使插件

    系统内置插件，当用户请求转告时，通过 planner 动作池选择转告动作，
    自动将消息转发到目标用户的私聊，并触发 LLM 思考。

    """

    # 插件基本信息
    plugin_name: str = "messenger"
    enable_plugin: bool = True
    dependencies: list[str] = []
    python_dependencies: list[str] = []
    config_file_name: str = "config.toml"

    # 配置节描述
    config_section_descriptions = {
        "plugin": "插件启用配置",
        "components": "组件配置",
    }

    # 配置Schema定义
    config_schema: dict = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="1.0.0", description="配置文件版本"),
        },
        "components": {
            "similarity_threshold": ConfigField(
                type=float,
                default=0.4,
                description="名称匹配最低相似度阈值（0-1）",
            ),
            "confirmation_timeout": ConfigField(
                type=int,
                default=60,
                description="转发确认等待超时（秒）",
            ),
            "mute_user_list": ConfigField(
                type=list,
                default=[],
                description="免打扰用户QQ号列表，列表中的用户禁止被转告",
            ),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件包含的组件列表"""
        components = []
        components.append((MessengerRelayAction.get_action_info(), MessengerRelayAction))
        components.append((ConveyCommand.get_command_info(), ConveyCommand))
        return components
