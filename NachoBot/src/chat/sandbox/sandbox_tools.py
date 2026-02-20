from typing import Any, List, Tuple
from src.plugin_system.base.base_tool import BaseTool
from src.plugin_system.base.component_types import ToolParamType
from src.chat.sandbox.sandbox_manager import sandbox_manager
from src.config.config import global_config
from src.plugin_system.core.component_registry import component_registry


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Read the content of a file from the current sandbox. Use this when you need to inspect a file uploaded by the user."
    available_for_llm = True
    parameters: List[Tuple[str, ToolParamType, str, bool, List[str] | None]] = [
        ("filename", ToolParamType.STRING, "The name of the file to read", True, None)
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        # ToolExecutor injects chat_stream
        chat_stream = getattr(self, "chat_stream", None)
        if not chat_stream:
            return {"error": "[Error: No chat context found. Cannot access sandbox.]"}

        stream_id = chat_stream.stream_id
        user_id = chat_stream.user_info.user_id

        # Security Check
        is_admin = str(user_id) in global_config.advanced.admins
        is_whitelisted = str(user_id) in global_config.bot.sandbox_whitelist

        if not (is_admin or is_whitelisted):
            return {"error": "[Error: You do not have permission to use Sandbox tools]"}

        filename = function_args.get("filename")
        if not filename:
            return {"error": "filename is required"}

        sandbox = sandbox_manager.get_sandbox(stream_id)
        content = sandbox.read_file(filename)

        if content is None:
            return {"error": f"File '{filename}' not found in sandbox."}

        return {"content": content}


class ListFilesTool(BaseTool):
    name = "list_files"
    description = "List all files currently in the sandbox."
    available_for_llm = True
    parameters: List[Tuple[str, ToolParamType, str, bool, List[str] | None]] = []

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        # ToolExecutor injects chat_stream
        chat_stream = getattr(self, "chat_stream", None)
        if not chat_stream:
            return {"error": "[Error: No chat context found. Cannot access sandbox.]"}

        stream_id = chat_stream.stream_id
        user_id = chat_stream.user_info.user_id

        # Security Check
        is_admin = str(user_id) in global_config.advanced.admins
        is_whitelisted = str(user_id) in global_config.bot.sandbox_whitelist

        if not (is_admin or is_whitelisted):
            return {"error": "[Error: You do not have permission to use Sandbox tools]"}

        sandbox = sandbox_manager.get_sandbox(stream_id)
        files = sandbox.list_files()

        if not files:
            return {"content": "(Sandbox is empty)"}

        return {"content": ", ".join(files)}


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "Write content to a file in the sandbox. Overwrites if exists."
    available_for_llm = True
    parameters: List[Tuple[str, ToolParamType, str, bool, List[str] | None]] = [
        ("filename", ToolParamType.STRING, "The name of the file to write", True, None),
        ("content", ToolParamType.STRING, "The content to write to the file", True, None),
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        # ToolExecutor injects chat_stream
        chat_stream = getattr(self, "chat_stream", None)
        if not chat_stream:
            return {"error": "[Error: No chat context found. Cannot access sandbox.]"}

        stream_id = chat_stream.stream_id
        user_id = chat_stream.user_info.user_id

        # Security Check
        is_admin = str(user_id) in global_config.advanced.admins
        is_whitelisted = str(user_id) in global_config.bot.sandbox_whitelist

        if not (is_admin or is_whitelisted):
            return {"error": "[Error: You do not have permission to use Sandbox tools]"}

        filename = function_args.get("filename")
        content = function_args.get("content")

        if not filename or content is None:
            return {"error": "filename and content are required."}

        try:
            sandbox = sandbox_manager.get_sandbox(stream_id)
            # save_file takes bytes for file_data if using existing method, or I can add write_file to Sandbox class.
            # Looking at sandbox_manager.py, save_file takes bytes.
            path = sandbox.save_file(content.encode("utf-8"), filename, overwrite=True)

            if path:
                # 自动将文件发送回给用户
                import os
                from src.plugin_system.apis import send_api

                abs_path = os.path.abspath(path)

                # 发送文件给用户
                await send_api.custom_to_stream(
                    message_type="file",
                    content=abs_path,
                    stream_id=stream_id,
                    display_message=f"已为您生成文件：{filename}",
                    typing=False,
                )

                return {"content": f"File '{filename}' written successfully and sent to chat."}
            else:
                return {"error": f"Failed to write file '{filename}'."}
        except Exception as e:
            return {"error": f"Error writing file: {e}"}


def register_sandbox_tools():
    """Register sandbox tools to the global component registry"""

    # We create a dummy 'core' plugin info if strictly needed, but let's see if we can register without it.
    # BaseTool.get_tool_info() creates a ToolInfo object.

    # Register ReadFileTool
    read_tool_info = ReadFileTool.get_tool_info()
    # Manually set enabled to True as it defaults to True in class but let's be safe
    read_tool_info.enabled = True
    component_registry.register_component(read_tool_info, ReadFileTool)

    # Register ListFilesTool
    list_tool_info = ListFilesTool.get_tool_info()
    list_tool_info.enabled = True
    component_registry.register_component(list_tool_info, ListFilesTool)

    # Register WriteFileTool
    write_tool_info = WriteFileTool.get_tool_info()
    write_tool_info.enabled = True
    component_registry.register_component(write_tool_info, WriteFileTool)
