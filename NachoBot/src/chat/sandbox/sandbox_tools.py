from typing import Any, List, Tuple
from src.plugin_system.base.base_tool import BaseTool
from src.plugin_system.base.component_types import ToolParamType
from src.chat.sandbox.sandbox_manager import sandbox_manager
from src.config.config import global_config
from src.plugin_system.core.component_registry import component_registry
from src.common.logger import get_logger

_sandbox_tools_logger = get_logger("sandbox_tools")


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

        # 从消息上下文获取真实发送者（群聊中 chat_stream.user_info 可能是流创建者）
        user_id = ""
        if chat_stream.context:
            msg_info = chat_stream.context.message.message_info
            if msg_info.sender_info and msg_info.sender_info.user_id:
                user_id = str(msg_info.sender_info.user_id)
            elif msg_info.user_info and msg_info.user_info.user_id:
                user_id = str(msg_info.user_info.user_id)
        if not user_id:
            user_id = str(chat_stream.user_info.user_id)

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

        # 异步触发 LLM 概括文件内容，存入 sandbox 供后续 replyer 注入
        import asyncio
        asyncio.create_task(self._summarize_and_store(sandbox, filename, content))

        return {"content": content}

    @staticmethod
    async def _summarize_and_store(sandbox, filename: str, content: str):
        """Use tool_use model to generate a concise summary of the file content"""
        from src.common.logger import get_logger
        from src.config.config import model_config
        from src.llm_models.utils_model import LLMRequest

        _logger = get_logger("sandbox_tools")
        try:
            # 截断过长内容以适配模型上下文 (tool_use 通常支持 32k)
            max_content_len = 15000
            truncated = content[:max_content_len]
            if len(content) > max_content_len:
                truncated += f"\n... (内容已截断，原始长度: {len(content)} 字符)"

            summary_prompt = (
                f"请概括以下文件的内容，用简洁的中文描述（200字以内），"
                f"重点说明文件的类型、结构、关键内容和用途。\n\n"
                f"文件名: {filename}\n"
                f"文件内容:\n{truncated}"
            )

            llm = LLMRequest(
                model_set=model_config.model_task_config.tool_use,
                request_type="file_summary",
            )
            summary_text, _ = await llm.generate_response_async(
                prompt=summary_prompt, max_tokens=512
            )

            if summary_text and summary_text.strip():
                sandbox.add_file_summary(filename, summary_text.strip(), rounds=3)
                _logger.info(f"[ReadFileTool] 文件概述生成成功: {filename} -> {summary_text[:80]}...")
            else:
                _logger.warning(f"[ReadFileTool] 文件概述生成为空: {filename}")
        except Exception as e:
            _logger.error(f"[ReadFileTool] 文件概述生成失败: {filename}, 错误: {e}")


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

        # 从消息上下文获取真实发送者（群聊中 chat_stream.user_info 可能是流创建者）
        user_id = ""
        if chat_stream.context:
            msg_info = chat_stream.context.message.message_info
            if msg_info.sender_info and msg_info.sender_info.user_id:
                user_id = str(msg_info.sender_info.user_id)
            elif msg_info.user_info and msg_info.user_info.user_id:
                user_id = str(msg_info.user_info.user_id)
        if not user_id:
            user_id = str(chat_stream.user_info.user_id)

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

        # 从消息上下文获取真实发送者（群聊中 chat_stream.user_info 可能是流创建者）
        user_id = ""
        if chat_stream.context:
            msg_info = chat_stream.context.message.message_info
            if msg_info.sender_info and msg_info.sender_info.user_id:
                user_id = str(msg_info.sender_info.user_id)
            elif msg_info.user_info and msg_info.user_info.user_id:
                user_id = str(msg_info.user_info.user_id)
        if not user_id:
            user_id = str(chat_stream.user_info.user_id)

        # Security Check
        is_admin = str(user_id) in global_config.advanced.admins
        is_whitelisted = str(user_id) in global_config.bot.sandbox_whitelist

        if not (is_admin or is_whitelisted):
            return {"error": "[Error: You do not have permission to use Sandbox tools]"}

        filename = function_args.get("filename")
        content = function_args.get("content")

        if not filename or content is None:
            return {"error": "filename and content are required."}

        # ── 防御性过滤：模型幻觉生成 "No tool needed" 文本文件 ──
        # tool_use 模型组有时会错误地调用 write_file，内容仅为 "No tool needed"
        # 这类文件没有实际意义，静默丢弃，不发送给用户
        import re
        content_stripped = content.strip().strip('"').strip("'").strip()
        if re.fullmatch(r"no\s+tool\s+needed\.?", content_stripped, re.IGNORECASE):
            _sandbox_tools_logger.warning(
                f"[WriteFileTool] write_file 内容为 '{content_stripped}'，"
                f"文件名 '{filename}'，已静默丢弃。"
            )
            return None

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
