import os
from typing import List, Tuple, Type

from src.plugin_system import BasePlugin, register_plugin, ComponentInfo
from src.plugin_system.base.config_types import ConfigField

from .event_handler import ModerationEventHandler


@register_plugin
class ModerationPlugin(BasePlugin):
    """
    智能撤回插件，基于正则和图片哈希过滤违规消息。
    """
    plugin_name = "moderation_plugin"
    plugin_description = "智能撤回插件，基于正则和图片哈希过滤违规消息。"
    enable_plugin = True
    dependencies: List[str] = []
    python_dependencies: List = []
    config_file_name = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本配置",
        "moderation": "违规过滤配置"
    }

    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="1.0.0", description="配置文件版本"),
        },
        "moderation": {
            "ban_regex_list": ConfigField(type=list, default=[], description="正则违规词列表"),
            "whitelist_qq": ConfigField(type=list, default=[], description="白名单QQ号列表"),
            "banned_images_dir": ConfigField(type=str, default="data/banned_images", description="违规图片库文件夹路径"),
            "recall_message": ConfigField(type=str, default="检测到违规内容，已自动撤回", description="撤回提示语")
        }
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        components = []
        if self.get_config("plugin.enabled", True):
            components.append((ModerationEventHandler.get_handler_info(), ModerationEventHandler))
        return components
