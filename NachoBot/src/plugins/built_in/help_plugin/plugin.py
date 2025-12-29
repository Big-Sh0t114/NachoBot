from typing import Dict, List, Optional, Tuple, Type
import re

from src.common.logger import get_logger
from src.plugin_system import BaseCommand, BasePlugin, ComponentInfo, register_plugin
from src.plugin_system.base.component_types import CommandInfo, ComponentType
from src.plugin_system.base.config_types import ConfigField
from src.chat.advanced.advanced_manager import advanced_manager
from src.plugin_system.core.component_registry import component_registry
from src.plugin_system.core.global_announcement_manager import global_announcement_manager

logger = get_logger("help_plugin")


class HelpCommand(BaseCommand):
    """处理 #help 指令，输出当前可用的指令列表"""

    command_name: str = "help"
    command_description: str = "查看当前可用指令及其作用"
    command_pattern: str = r"(?i)^#help$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.message or not self.message.chat_stream:
            return False, "无法获取聊天信息", True

        stream_id = self.message.chat_stream.stream_id
        user_info = getattr(getattr(self.message, "message_info", None), "user_info", None)
        user_id = str(user_info.user_id) if user_info and getattr(user_info, "user_id", None) else None

        grouped_entries = self._collect_commands(stream_id, user_id, show_admin=False, skip_names={"help", "help_all"})

        if not grouped_entries["public"] and not grouped_entries["whitelist"]:
            await self.send_text("当前没有可用的指令。")
            return True, "no commands", True

        lines: List[str] = ["可用指令："]
        if grouped_entries["public"]:
            lines.append("[所有人可用]")
            lines.extend([f"- {pattern}：{desc}" for pattern, desc in grouped_entries["public"]])
        if grouped_entries["whitelist"]:
            lines.append("[需要白名单]")
            lines.extend([f"- {pattern}：{desc}" for pattern, desc in grouped_entries["whitelist"]])

        await self.send_text("\n".join(lines))
        return True, "help sent", True

    def _collect_commands(
        self,
        stream_id: Optional[str],
        user_id: Optional[str],
        show_admin: bool = False,
        skip_names: Optional[set[str]] = None,
    ) -> Dict[str, List[Tuple[str, str]]]:
        """收集当前可用的命令信息，并按权限分组"""
        skip_names = skip_names or set()
        enabled_commands = component_registry.get_enabled_components_by_type(ComponentType.COMMAND)
        disabled_commands = set(global_announcement_manager.get_disabled_chat_commands(stream_id)) if stream_id else set()
        existing_names = set(enabled_commands.keys())

        public_entries: List[Tuple[str, str]] = []
        whitelist_entries: List[Tuple[str, str]] = []
        admin_entries: List[Tuple[str, str]] = []
        existing_patterns: set[str] = set()

        for name, info in enabled_commands.items():
            if name in disabled_commands:
                continue
            if name in skip_names:
                continue  # 不显示本指令或指定跳过的指令

            pattern_raw = getattr(info, "command_pattern", "") or name
            pattern = self._format_pattern_for_display(pattern_raw)
            desc = info.description or "暂无描述"

            permission = self._classify_permission(name, info, user_id)
            if permission == "admin" and not show_admin:
                continue  # 默认隐藏管理员指令

            if permission == "admin":
                target_list = admin_entries
            elif permission == "whitelist":
                target_list = whitelist_entries
            else:
                target_list = public_entries
            desc = desc + self._scope_suffix(name, pattern)
            target_list.append((pattern, desc))
            existing_patterns.add(pattern)

        # 手工补充/过滤
        if self.get_config("display.include_manual_commands", True):
            self._append_manual_commands(
                disabled_commands=disabled_commands,
                existing_patterns=existing_patterns,
                existing_names=existing_names,
                public_entries=public_entries,
                whitelist_entries=whitelist_entries,
                admin_entries=admin_entries,
                show_admin=show_admin,
                user_id=user_id,
            )

        public_entries.sort(key=lambda item: item[0].lower())
        whitelist_entries.sort(key=lambda item: item[0].lower())
        admin_entries = sorted(admin_entries, key=lambda item: item[0].lower())

        return {"public": public_entries, "whitelist": whitelist_entries, "admin": admin_entries}

    def _classify_permission(self, name: str, info: CommandInfo, user_id: Optional[str]) -> str:
        """根据已知规则粗略判断权限需求"""
        meta_perm = getattr(info, "metadata", {}).get("permission")
        if meta_perm in {"admin", "whitelist", "public"}:
            return meta_perm
        # 高级模式开关：需要白名单
        if name in {"advanced_enable", "advanced_disable"}:
            return "whitelist"
        # 高级模式检查：仅管理员，直接过滤
        if name in {"advanced_check"}:
            return "admin"
        # Maizone 发说说指令：需要配置权限，视为白名单
        if name in {"send_post", "send_feed"}:
            return "whitelist"
        # 默认公共
        return "public"

    def _manual_commands(
        self, disabled_commands: set[str], existing_patterns: set[str], existing_names: set[str]
    ) -> List[Tuple[str, str]]:
        """补充未注册到组件系统但仍可用的内建指令"""
        existing_patterns_lower = {p.lower() for p in existing_patterns}
        stripped_patterns = {p.lower().strip("^$") for p in existing_patterns}

        manual = [
            ("#adv_on", "开启高级模式（会停用联网和TTS功能）"),
            ("#adv_off", "关闭高级模式"),
        ]
        extras: List[Tuple[str, str]] = []
        for pattern, desc in manual:
            normalized = pattern.lower().lstrip("#")
            if (
                pattern in existing_patterns
                or normalized in existing_names
                or pattern.lower() in existing_patterns_lower
                or normalized in stripped_patterns
                or pattern in disabled_commands
                or normalized in disabled_commands
            ):
                continue
            extras.append((pattern, desc))
        return extras

    def _append_manual_commands(
        self,
        disabled_commands: set[str],
        existing_patterns: set[str],
        existing_names: set[str],
        public_entries: List[Tuple[str, str]],
        whitelist_entries: List[Tuple[str, str]],
        admin_entries: List[Tuple[str, str]],
        show_admin: bool,
        user_id: Optional[str],
    ) -> None:
        """补充手工指令，并将日记命令按权限拆分"""

        # 内建未注册指令（高级模式开关，白名单）
        for pattern, desc in self._manual_commands(disabled_commands, existing_patterns, existing_names):
            display_pattern = self._format_pattern_for_display(pattern)
            if display_pattern in existing_patterns or pattern in disabled_commands:
                continue
            desc = desc + self._scope_suffix(display_pattern, display_pattern)
            whitelist_entries.append((display_pattern, desc))
            existing_patterns.add(display_pattern)

        # 日记命令：仅展示所有人可用的 #diary_view，隐藏管理员子命令
        diary_enabled = self._is_diary_command_enabled()
        diary_view_pattern = "#diary_view"
        if diary_enabled and diary_view_pattern not in existing_patterns and diary_view_pattern not in disabled_commands:
            desc = "查看日记内容" + self._scope_suffix("diary_view", diary_view_pattern)
            public_entries.append((diary_view_pattern, desc))
            existing_patterns.add(diary_view_pattern)

        # 管理员指令：手工加入 #adv_check
        if show_admin and user_id and advanced_manager.is_admin(user_id):
            adv_check_pattern = "#adv_check"
            if adv_check_pattern not in existing_patterns and adv_check_pattern not in disabled_commands:
                desc = "查看开启高级模式的用户列表" + self._scope_suffix("adv_check", adv_check_pattern)
                admin_entries.append((adv_check_pattern, desc))
                existing_patterns.add(adv_check_pattern)

    def _is_diary_command_enabled(self) -> bool:
        """检测日记插件命令是否开启"""
        diary_config = component_registry.get_plugin_config("diary_plugin") or {}
        plugin_cfg = diary_config.get("plugin", {})
        enabled = plugin_cfg.get("enabled", True)
        enable_cmd = plugin_cfg.get("enable_command", True)
        return bool(enabled and enable_cmd)

    def _format_pattern_for_display(self, pattern: str) -> str:
        """将正则模式转成更易懂的触发格式"""
        if not pattern:
            return pattern

        # 针对已知命令做映射，避免看到正则符号
        if "lang_switch" in pattern:
            return "#lang_switch"
        if "mus_rand" in pattern:
            return "#mus_rand"
        if "点歌|播放|来首" in pattern:
            return "点歌/播放/来首 + 关键词"
        if "diary_generate_all" in pattern:
            return "#diary_generate_all"
        if "diary_generate" in pattern:
            return "#diary_generate"
        if "diary_list" in pattern:
            return "#diary_list"
        if "diary_view" in pattern:
            return "#diary_view"
        if "diary_debug" in pattern:
            return "#diary_debug"
        if "diary_help" in pattern:
            return "#diary_help"
        if "advanced_enable" in pattern or "#adv_on" in pattern:
            return "#adv_on"
        if "advanced_disable" in pattern or "#adv_off" in pattern:
            return "#adv_off"
        if "advanced_check" in pattern or "#adv_check" in pattern:
            return "#adv_check"

        tag_match = re.search(r"#\w[\w_]*", pattern)
        if tag_match:
            return tag_match.group(0)

        cleaned = pattern
        cleaned = cleaned.replace("(?i)", "")
        cleaned = re.sub(r"\(\?P<[^>]+>", "(", cleaned)
        cleaned = cleaned.replace("?:", "")
        cleaned = re.sub(r"^\s*\^\\s*|\^\s*", "", cleaned)
        cleaned = re.sub(r"\\s*\$\s*$|\$\s*$", "", cleaned)
        cleaned = cleaned.strip("^$")
        cleaned = cleaned.replace(r"\s*", " ")
        cleaned = cleaned.replace(r"\s+", " ")
        cleaned = cleaned.replace("\\", "")
        cleaned = cleaned.replace("|", "/")
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        return cleaned.strip()

    def _scope_suffix(self, command_name: str, pattern: str) -> str:
        """根据配置的作用域标识返回后缀"""
        group_only = [str(c).lower() for c in self.get_config("scope.group_only", []) or []]
        private_only = [str(c).lower() for c in self.get_config("scope.private_only", []) or []]

        key_candidates = {command_name.lower(), pattern.lower()}
        if any(k in group_only for k in key_candidates):
            return "（仅群聊）"
        if any(k in private_only for k in key_candidates):
            return "（仅私聊）"
        return ""


class HelpAllCommand(HelpCommand):
    """处理 #help_all 指令，包含管理员指令"""

    command_name: str = "help_all"
    command_description: str = "查看全部指令（含管理员）"
    command_pattern: str = r"(?i)^#help_all$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.message or not self.message.chat_stream:
            return False, "无法获取聊天信息", True

        stream_id = self.message.chat_stream.stream_id
        user_info = getattr(getattr(self.message, "message_info", None), "user_info", None)
        user_id = str(user_info.user_id) if user_info and getattr(user_info, "user_id", None) else None

        grouped_entries = self._collect_commands(stream_id, user_id, show_admin=True, skip_names={"help", "help_all"})

        if not any(grouped_entries.values()):
            await self.send_text("当前没有可用的指令。")
            return True, "no commands", True

        lines: List[str] = ["可用指令（含管理员）："]
        if grouped_entries["public"]:
            lines.append("[所有人可用]")
            lines.extend([f"- {pattern}：{desc}" for pattern, desc in grouped_entries["public"]])
        if grouped_entries["whitelist"]:
            lines.append("[需要白名单/特权]")
            lines.extend([f"- {pattern}：{desc}" for pattern, desc in grouped_entries["whitelist"]])
        if grouped_entries.get("admin"):
            lines.append("[管理员]")
            lines.extend([f"- {pattern}：{desc}" for pattern, desc in grouped_entries["admin"]])

        await self.send_text("\n".join(lines))
        return True, "help all sent", True


@register_plugin
class HelpPlugin(BasePlugin):
    """帮助指令插件：提供 #help 命令"""

    plugin_name: str = "help_plugin"
    enable_plugin: bool = True
    dependencies: list[str] = []
    python_dependencies: list[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件启用和版本配置",
        "display": "帮助指令的展示细节配置",
        "scope": "指令适用范围配置（群聊/私聊）",
    }

    config_schema: dict = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用帮助指令插件"),
            "config_version": ConfigField(type=str, default="1.0.0", description="配置文件版本"),
        },
        "display": {
            "show_plugin_name": ConfigField(type=bool, default=True, description="在帮助中显示指令所属插件"),
            "include_manual_commands": ConfigField(
                type=bool, default=True, description="是否展示未注册但可用的内建指令（如高级模式开关）"
            ),
        },
        "scope": {
            "group_only": ConfigField(type=list, default=[], description="仅群聊可用的指令名称或触发词"),
            "private_only": ConfigField(type=list, default=[], description="仅私聊可用的指令名称或触发词"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo | CommandInfo, Type]]:
        components: List[Tuple[ComponentInfo | CommandInfo, Type]] = []
        components.append((HelpCommand.get_command_info(), HelpCommand))
        components.append((HelpAllCommand.get_command_info(), HelpAllCommand))
        return components
