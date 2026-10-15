"""Isolated integration checks for help collection and rendered responses."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


HELP_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "src" / "plugins" / "built_in" / "help_plugin"


class _FakeRegistry:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def get_enabled_components_by_type(self, _component_type: object) -> dict[str, object]:
        return self.commands

    def get_plugin_config(self, name: str) -> dict:
        if name == "diary_plugin":
            return {"plugin": {"enabled": False, "enable_command": False}}
        return {}


class _FakeAnnouncements:
    def __init__(self) -> None:
        self.disabled_commands: set[str] = set()

    def get_disabled_chat_commands(self, _stream_id: str) -> set[str]:
        return self.disabled_commands


def _load_help_module():
    """Load the real help module with external runtime imports replaced by stubs."""
    registry = _FakeRegistry()
    announcements = _FakeAnnouncements()

    class BaseCommand:
        async def send_text(self, text: str) -> None:
            self.sent_text = text

        def get_config(self, key: str, default=None):
            return getattr(self, "config", {}).get(key, default)

    class BasePlugin:
        pass

    class ConfigField:
        def __init__(self, **_kwargs) -> None:
            pass

    class ComponentType:
        COMMAND = "command"

    class ComponentInfo:
        pass

    class CommandInfo:
        pass

    class AdvancedManager:
        def is_admin(self, _user_id: str) -> bool:
            return False

    def package(name: str) -> types.ModuleType:
        value = types.ModuleType(name)
        value.__path__ = []
        return value

    def module(name: str, **attributes) -> types.ModuleType:
        value = types.ModuleType(name)
        for attribute, content in attributes.items():
            setattr(value, attribute, content)
        return value

    component_types = module(
        "src.plugin_system.base.component_types",
        CommandInfo=CommandInfo,
        ComponentType=ComponentType,
    )
    config_types = module("src.plugin_system.base.config_types", ConfigField=ConfigField)
    base_command = module(
        "src.plugin_system.base.base_command",
        SwitchGpRpyCommand=type("SwitchGpRpyCommand", (), {}),
        SwitchPrRpyCommand=type("SwitchPrRpyCommand", (), {}),
    )
    component_registry = module("src.plugin_system.core.component_registry", component_registry=registry)
    announcement_manager = module(
        "src.plugin_system.core.global_announcement_manager",
        global_announcement_manager=announcements,
    )
    advanced_manager = module("src.chat.advanced.advanced_manager", advanced_manager=AdvancedManager())
    stubs = {
        "src.common": package("src.common"),
        "src.common.logger": module("src.common.logger", get_logger=lambda _name: object()),
        "src.plugin_system": module(
            "src.plugin_system",
            BaseCommand=BaseCommand,
            BasePlugin=BasePlugin,
            ComponentInfo=ComponentInfo,
            register_plugin=lambda cls: cls,
        ),
        "src.plugin_system.base": package("src.plugin_system.base"),
        "src.plugin_system.base.component_types": component_types,
        "src.plugin_system.base.config_types": config_types,
        "src.plugin_system.base.base_command": base_command,
        "src.plugin_system.core": package("src.plugin_system.core"),
        "src.plugin_system.core.component_registry": component_registry,
        "src.plugin_system.core.global_announcement_manager": announcement_manager,
        "src.chat": package("src.chat"),
        "src.chat.advanced": package("src.chat.advanced"),
        "src.chat.advanced.advanced_manager": advanced_manager,
    }

    package_name = "_isolated_help_plugin"
    plugin_name = f"{package_name}.plugin"
    with patch.dict(sys.modules, stubs):
        sys.modules[package_name] = package(package_name)
        sys.modules[package_name].__path__ = [str(HELP_PLUGIN_DIR)]
        spec = importlib.util.spec_from_file_location(plugin_name, HELP_PLUGIN_DIR / "plugin.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load help_plugin for isolated integration tests")
        plugin_module = importlib.util.module_from_spec(spec)
        sys.modules[plugin_name] = plugin_module
        spec.loader.exec_module(plugin_module)
    return plugin_module, registry, announcements


class HelpCommandIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin_module, cls.registry, cls.announcements = _load_help_module()

    def setUp(self) -> None:
        qqcall_pattern = (
            r"(?i)^[ 　]*(?:#(?P<operation>join|leave|stat)_vc_qq"
            r"|#qqcall\s+(?P<private_operation>accept|hangup|reject)"
            r"|\[语音：[ 　]*#join_vc_qq[ 　]*\])[ 　]*$"
        )
        self.registry.commands = {
            "qqcall_control": self._info(qqcall_pattern, "通话控制"),
            "whitelisted": self._info(r"^#white$", "白名单指令", "whitelist"),
            "admin_control": self._info(r"^#admin$", "管理员指令", "admin"),
            "disabled_command": self._info(r"^#disabled$", "已禁用"),
            "convey": self._info(r"^#convey$", "不显示的内建命令"),
            "help": self._info(r"^#help$", "帮助"),
            "help_all": self._info(r"^#help_all$", "全部帮助"),
        }
        self.announcements.disabled_commands = {"disabled_command"}
        self.command = self.plugin_module.HelpCommand()
        self.command.message = types.SimpleNamespace(
            chat_stream=types.SimpleNamespace(stream_id="test-stream"),
            message_info=types.SimpleNamespace(user_info=types.SimpleNamespace(user_id="user-1")),
        )
        self.command.config = {
            "display.include_manual_commands": False,
            "scope.group_only": [],
            "scope.private_only": ["qqcall_control"],
        }

    @staticmethod
    def _info(pattern: str, description: str, permission: str = "public") -> types.SimpleNamespace:
        return types.SimpleNamespace(
            command_pattern=pattern,
            description=description,
            metadata={"permission": permission},
        )

    async def test_collect_commands_preserves_permissions_disabled_and_scope_rules(self) -> None:
        entries = self.command._collect_commands("test-stream", "user-1", skip_names={"help", "help_all"})
        self.assertEqual(
            entries["public"],
            [
                (
                    "#join_vc_qq/#leave_vc_qq/#stat_vc_qq/#qqcall accept/#qqcall hangup/#qqcall reject/[语音：#join_vc_qq]",
                    "通话控制（仅私聊）",
                )
            ],
        )
        self.assertEqual(entries["whitelist"], [("#white", "白名单指令")])
        self.assertEqual(entries["admin"], [])

    async def test_help_hides_admin_and_disabled_commands_but_uses_formatted_triggers(self) -> None:
        result = await self.command.execute()
        self.assertEqual(result, (True, "help sent", True))
        self.assertIn("#join_vc_qq/#leave_vc_qq/#stat_vc_qq", self.command.sent_text)
        self.assertIn("通话控制（仅私聊）", self.command.sent_text)
        self.assertIn("[需要白名单]", self.command.sent_text)
        self.assertNotIn("#admin", self.command.sent_text)
        self.assertNotIn("#disabled", self.command.sent_text)
        self.assertNotIn("#convey", self.command.sent_text)

    async def test_help_all_includes_admin_commands_and_still_filters_disabled_commands(self) -> None:
        command = self.plugin_module.HelpAllCommand()
        command.message = self.command.message
        command.config = self.command.config
        result = await command.execute()
        self.assertEqual(result, (True, "help all sent", True))
        self.assertIn("[管理员]", command.sent_text)
        self.assertIn("#admin：管理员指令", command.sent_text)
        self.assertNotIn("#disabled", command.sent_text)


if __name__ == "__main__":
    unittest.main()
