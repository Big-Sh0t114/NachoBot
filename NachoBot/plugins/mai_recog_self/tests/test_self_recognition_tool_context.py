"""Tests for opt-in, invocation-local ToolExecutor preparation contexts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any


CORE_ROOT = Path(__file__).resolve().parents[3]
TOOL_USE_PATH = CORE_ROOT / "src" / "plugin_system" / "core" / "tool_use.py"


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


@dataclass
class ToolExecutionPreparation:
    prompt_supplement: str = ""
    context: Any = None
    cache_sensitive: bool = True


@dataclass
class ToolCall:
    call_id: str
    func_name: str
    args: dict


class BaseTool:
    def __init__(self, _config=None):
        self.chat_stream = None


class Prompt:
    def __init__(self, template, _name, _should_register=False):
        self.template = template


class PromptManager:
    def register(self, _prompt):
        return None

    async def format_prompt(self, _name, **values):
        return "decision for {sender}: {target_message}\n{chat_history}".format(**values)


class FakeLLM:
    def __init__(self, tool_call, barrier=None):
        self.tool_call = tool_call
        self.barrier = barrier
        self.prompts = []
        self.calls = 0
        self.model_for_task = SimpleNamespace(model_list=["stub-model"])

    async def generate_response_async(self, *, prompt, tools, raise_when_empty=False):
        self.calls += 1
        self.prompts.append(prompt)
        if self.barrier is not None:
            self.barrier["count"] += 1
            if self.barrier["count"] == 2:
                self.barrier["event"].set()
            await self.barrier["event"].wait()
        return "", (None, "stub", [self.tool_call])


@contextmanager
def isolated_tool_executor():
    names = (
        "src",
        "src.plugin_system",
        "src.plugin_system.apis",
        "src.plugin_system.apis.tool_api",
        "src.plugin_system.base",
        "src.plugin_system.base.base_tool",
        "src.plugin_system.base.tool_execution_context",
        "src.plugin_system.core",
        "src.plugin_system.core.component_registry",
        "src.plugin_system.core.global_announcement_manager",
        "src.llm_models",
        "src.llm_models.utils_model",
        "src.llm_models.payload_content",
        "src.config",
        "src.config.config",
        "src.chat",
        "src.chat.utils",
        "src.chat.utils.prompt_builder",
        "src.chat.message_receive",
        "src.chat.message_receive.chat_stream",
        "src.common",
        "src.common.logger",
        "_isolated_tool_use",
    )
    previous = {name: sys.modules.get(name) for name in names}

    def module(name, **attrs):
        result = types.ModuleType(name)
        result.__dict__.update(attrs)
        return result

    for name in (
        "src",
        "src.plugin_system",
        "src.plugin_system.apis",
        "src.plugin_system.base",
        "src.plugin_system.core",
        "src.llm_models",
        "src.llm_models.payload_content",
        "src.config",
        "src.chat",
        "src.chat.utils",
        "src.chat.message_receive",
        "src.common",
    ):
        package = module(name)
        package.__path__ = []
        sys.modules[name] = package

    state = SimpleNamespace(
        definitions=[],
        tool_classes={},
        streams={},
        disabled=set(),
        instances_created=0,
    )

    def get_tool_instance(name):
        tool_class = state.tool_classes.get(name)
        if tool_class is None:
            return None
        state.instances_created += 1
        return tool_class({})

    sys.modules["src.plugin_system.apis.tool_api"] = module(
        "src.plugin_system.apis.tool_api",
        get_llm_available_tool_definitions=lambda: state.definitions,
        get_tool_instance=get_tool_instance,
    )
    sys.modules["src.plugin_system.base.base_tool"] = module(
        "src.plugin_system.base.base_tool", BaseTool=BaseTool
    )
    sys.modules["src.plugin_system.base.tool_execution_context"] = module(
        "src.plugin_system.base.tool_execution_context", ToolExecutionPreparation=ToolExecutionPreparation
    )
    registry = SimpleNamespace(get_llm_available_tools=lambda: state.tool_classes)
    sys.modules["src.plugin_system.core.component_registry"] = module(
        "src.plugin_system.core.component_registry", component_registry=registry
    )
    announcement = SimpleNamespace(get_disabled_chat_tools=lambda _chat_id: state.disabled)
    sys.modules["src.plugin_system.core.global_announcement_manager"] = module(
        "src.plugin_system.core.global_announcement_manager", global_announcement_manager=announcement
    )
    sys.modules["src.llm_models.utils_model"] = module(
        "src.llm_models.utils_model", LLMRequest=lambda **_kwargs: None
    )
    sys.modules["src.llm_models.payload_content"] = module("src.llm_models.payload_content", ToolCall=ToolCall)
    config = SimpleNamespace(
        bot=SimpleNamespace(nickname="Bot"),
        model_task_config=SimpleNamespace(tool_use=object()),
    )
    sys.modules["src.config.config"] = module(
        "src.config.config", global_config=config, model_config=config
    )
    sys.modules["src.chat.utils.prompt_builder"] = module(
        "src.chat.utils.prompt_builder", Prompt=Prompt, global_prompt_manager=PromptManager()
    )

    class ChatManager:
        def get_stream(self, chat_id):
            return state.streams[chat_id]

        def get_stream_name(self, chat_id):
            return chat_id

    sys.modules["src.chat.message_receive.chat_stream"] = module(
        "src.chat.message_receive.chat_stream", get_chat_manager=lambda: ChatManager()
    )
    sys.modules["src.common.logger"] = module("src.common.logger", get_logger=lambda _name: _Logger())

    spec = importlib.util.spec_from_file_location("_isolated_tool_use", TOOL_USE_PATH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    try:
        yield loaded, state
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class ToolExecutionContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_contexts_are_local_and_bypass_executor_cache(self):
        with isolated_tool_executor() as (module, state):
            class ContextTool(BaseTool):
                name = "recognize_self_image"

                async def prepare_execution_context(self, stream):
                    await asyncio.sleep(0.01)
                    context = {"stream_id": stream.stream_id, "message_id": stream.message_id}
                    supplement = f"canonical message_id={stream.message_id}; stream={stream.stream_id}"
                    return ToolExecutionPreparation(supplement, context, cache_sensitive=True)

                async def execute(self, _args):
                    raise AssertionError("context tool must execute with its invocation snapshot")

                async def execute_with_context(self, _args, context):
                    return {"content": json.dumps(context)}

            state.tool_classes = {"recognize_self_image": ContextTool}
            state.definitions = [("recognize_self_image", {"name": "recognize_self_image"})]
            state.streams = {
                "chat-a": SimpleNamespace(stream_id="chat-a", message_id="canonical-a"),
                "chat-b": SimpleNamespace(stream_id="chat-b", message_id="canonical-b"),
            }
            barrier = {"count": 0, "event": asyncio.Event()}
            executors = []
            for chat_id in ("chat-a", "chat-b"):
                executor = module.ToolExecutor(chat_id, enable_cache=True)
                executor.llm_model = FakeLLM(ToolCall("call", "recognize_self_image", {"message_id": "chosen"}), barrier)
                executors.append(executor)

            async def invoke(executor):
                return await executor.execute_from_chat_message("look at that", "same history", "user", True)

            results = await asyncio.gather(*(invoke(executor) for executor in executors))
            for index, (results_for_chat, _used, prompt) in enumerate(results):
                expected_stream = ("chat-a", "chat-b")[index]
                expected_message = ("canonical-a", "canonical-b")[index]
                observed = json.loads(results_for_chat[0]["content"])
                self.assertEqual(observed, {"stream_id": expected_stream, "message_id": expected_message})
                self.assertIn(expected_message, prompt)
                self.assertNotIn(("canonical-b", "canonical-a")[index], prompt)
                self.assertEqual(executors[index].tool_cache, {})

            # Same content/history still runs again: media/context-sensitive tools cannot use old cache entries.
            await executors[0].execute_from_chat_message("look at that", "same history", "user")
            self.assertEqual(executors[0].llm_model.calls, 2)
            self.assertEqual(executors[0].tool_cache, {})

    async def test_non_context_tools_keep_execute_and_cache_behavior(self):
        with isolated_tool_executor() as (module, state):
            class EchoTool(BaseTool):
                name = "echo_tool"
                calls = 0

                async def execute(self, _args):
                    type(self).calls += 1
                    return {"content": "ordinary result"}

            state.tool_classes = {"echo_tool": EchoTool}
            state.definitions = [("echo_tool", {"name": "echo_tool"})]
            state.streams = {"chat": SimpleNamespace(stream_id="chat", message_id="not-used")}
            executor = module.ToolExecutor("chat", enable_cache=True)
            executor.llm_model = FakeLLM(ToolCall("call", "echo_tool", {}))

            first = await executor.execute_from_chat_message("same", "history", "user")
            second = await executor.execute_from_chat_message("same", "history", "user")

            self.assertEqual(first[0][0]["content"], "ordinary result")
            self.assertEqual(second, first)
            self.assertEqual(executor.llm_model.calls, 1)
            self.assertEqual(EchoTool.calls, 1)
            self.assertTrue(executor.tool_cache)


if __name__ == "__main__":
    unittest.main()
