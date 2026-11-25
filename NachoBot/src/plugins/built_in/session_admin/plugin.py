"""管理员命令：清理当前会话上下文并热重载配置。"""

from __future__ import annotations

from typing import List, Tuple, Type

from src.chat.advanced.advanced_manager import advanced_manager
from src.common.logger import get_logger
from src.plugin_system import BaseCommand, BasePlugin, CommandInfo, register_plugin


logger = get_logger("session_admin")


class _SessionAdminCommand(BaseCommand):
    """Shared authorization and safe acknowledgement behavior."""

    @classmethod
    def get_command_info(cls) -> CommandInfo:
        info = super().get_command_info()
        info.metadata["permission"] = "admin"
        return info

    @staticmethod
    def _is_recovered_message(message) -> bool:
        # Adapter-replayed history is still dispatched through normal command
        # handling. Never re-run an operator action or acknowledge stale history.
        from src.chat.offline_recovery import is_offline_recovery_row

        return is_offline_recovery_row(message)

    async def _acknowledge(self, text: str) -> None:
        try:
            await self.send_text(text, storage_message=False)
        except Exception:
            logger.exception("发送会话管理命令确认消息失败")

    async def execute(self) -> Tuple[bool, str | None, bool]:
        try:
            try:
                if self._is_recovered_message(self.message):
                    return True, None, True
            except Exception:
                # Fail closed if replay metadata cannot be inspected.
                logger.exception("无法检查会话管理命令的历史重放标记")
                return True, None, True

            user_info = getattr(getattr(self.message, "message_info", None), "user_info", None)
            user_id = getattr(user_info, "user_id", None) if user_info is not None else None
            if not advanced_manager.is_admin(str(user_id) if user_id is not None else None):
                await self._acknowledge("只有管理员可以使用此命令。")
                return True, None, True

            chat_stream = getattr(self.message, "chat_stream", None)
            stream_id = getattr(chat_stream, "stream_id", None) if chat_stream is not None else None
            if not stream_id:
                await self._acknowledge("无法获取当前会话信息，操作失败。")
                return False, "无法获取当前会话信息", True

            self.run_operation(str(stream_id))
            await self._acknowledge(self.success_message)
            return True, None, True
        except Exception:
            # Keep runtime details and configuration values out of the reply.
            logger.exception("执行会话管理命令失败: %s", self.command_name)
            await self._acknowledge("操作失败，请检查日志。")
            return False, "操作失败", True

    def run_operation(self, stream_id: str) -> None:
        raise NotImplementedError

    success_message = ""


class ClearContextCommand(_SessionAdminCommand):
    command_name = "clear_context"
    command_description = "清除当前会话的短期上下文（管理员）"
    command_pattern = r"(?i)^\s*#clear_context\s*$"

    success_message = "已清除当前会话的短期上下文。"

    def run_operation(self, stream_id: str) -> None:
        from src.chat.utils.short_term_context import clear_short_term_context

        clear_short_term_context(stream_id)


class ConfigReloadCommand(_SessionAdminCommand):
    command_name = "config_reload"
    command_description = "热重载 bot_config 和 model_config（管理员）"
    command_pattern = r"(?i)^\s*#config_reload\s*$"

    success_message = "已重新加载 Bot 和模型配置。"

    def run_operation(self, _stream_id: str) -> None:
        from src.config.config import reload_bot_and_model_config

        reload_bot_and_model_config()


@register_plugin
class SessionAdminPlugin(BasePlugin):
    """默认启用的会话管理命令插件。"""

    plugin_name: str = "session_admin"
    enable_plugin: bool = True
    dependencies: list[str] = []
    python_dependencies: list[str] = []
    config_file_name: str = ""
    config_schema: dict = {}

    def get_plugin_components(self) -> List[Tuple[CommandInfo, Type[BaseCommand]]]:
        return [
            (ClearContextCommand.get_command_info(), ClearContextCommand),
            (ConfigReloadCommand.get_command_info(), ConfigReloadCommand),
        ]
