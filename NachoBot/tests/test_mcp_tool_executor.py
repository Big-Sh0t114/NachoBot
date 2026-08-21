from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest


class _NullLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _PromptManager:
    async def format_prompt(self, _template: str, **kwargs):
        return f"TASK: {kwargs['target_message']}"


class _StubToolExecutor:
    pass


class _ToolCall:
    def __init__(self, func_name: str, args: dict):
        self.func_name = func_name
        self.args = args


class _FakeLLM:
    def __init__(self):
        self.prompts: list[str] = []
        self._rounds = [
            [_ToolCall("mcp_calendar_list", {"day": "tomorrow"})],
            [_ToolCall("mcp_calendar_detail", {"event_id": "evt-1"})],
            [],
        ]

    async def generate_response_async(self, *, prompt: str, tools, raise_when_empty: bool):
        self.prompts.append(prompt)
        calls = self._rounds[len(self.prompts) - 1]
        return "", ("", "fake-model", calls)


class _FakeService:
    def get_tool_definitions(self, _context=None):
        return [
            {"name": "mcp_calendar_list", "description": "List calendar events"},
            {"name": "mcp_calendar_detail", "description": "Read calendar event details"},
        ]


def _install_package(name: str) -> None:
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module


def _install_module(name: str, **attributes) -> None:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module


_PACKAGE_NAMES = (
    "src",
    "src.chat",
    "src.chat.utils",
    "src.common",
    "src.config",
    "src.llm_models",
    "src.mcp",
    "src.plugin_system",
    "src.plugin_system.core",
)
_DEPENDENCY_MODULE_NAMES = (
    "src.chat.utils.prompt_builder",
    "src.common.logger",
    "src.config.config",
    "src.llm_models.payload_content",
    "src.mcp.service",
    "src.mcp.types",
    "src.plugin_system.core.tool_use",
)
_MISSING = object()
_PREVIOUS_MODULES = {name: sys.modules.get(name, _MISSING) for name in (*_PACKAGE_NAMES, *_DEPENDENCY_MODULE_NAMES)}

for package_name in _PACKAGE_NAMES:
    _install_package(package_name)

_install_module("src.chat.utils.prompt_builder", global_prompt_manager=_PromptManager())
_install_module("src.common.logger", get_logger=lambda _name: _NullLogger())
_install_module(
    "src.config.config",
    global_config=types.SimpleNamespace(bot=types.SimpleNamespace(nickname="bot")),
    model_config=types.SimpleNamespace(),
    mcp_config=types.SimpleNamespace(
        mcp=types.SimpleNamespace(
            max_rounds=3,
            max_calls=5,
            max_candidate_tools=32,
            observation_max_chars=12000,
        )
    ),
)
_install_module("src.llm_models.payload_content", ToolCall=_ToolCall)
_install_module("src.mcp.service", MCPService=object, mcp_service=_FakeService())
_install_module("src.mcp.types", MCPAccessContext=object)
_install_module("src.plugin_system.core.tool_use", ToolExecutor=_StubToolExecutor)

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "src" / "plugin_system" / "core" / "mcp_tool_executor.py"
MODULE_SPEC = importlib.util.spec_from_file_location("mcp_tool_executor_under_test", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"Unable to load MCP tool executor from {MODULE_PATH}")
MCP_TOOL_EXECUTOR = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = MCP_TOOL_EXECUTOR
try:
    MODULE_SPEC.loader.exec_module(MCP_TOOL_EXECUTOR)
finally:
    for module_name, previous_module in _PREVIOUS_MODULES.items():
        if previous_module is _MISSING:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module


class MCPToolExecutorLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_router_catalog_keeps_large_tool_set_visible(self):
        executor = MCP_TOOL_EXECUTOR.MCPToolExecutor.__new__(MCP_TOOL_EXECUTOR.MCPToolExecutor)
        executor.mcp_service = types.SimpleNamespace(
            get_tool_definitions=lambda _context=None: [
                {
                    "name": f"mcp_server_tool_{index:02d}",
                    "description": "A deliberately long tool description " * 12,
                }
                for index in range(41)
            ]
        )

        catalog = executor.get_tool_catalog_summary(max_chars=6000)

        self.assertIn("mcp_server_tool_00", catalog)
        self.assertIn("mcp_server_tool_40", catalog)
        self.assertNotIn("additional MCP tools omitted", catalog)

    def test_router_candidates_do_not_expose_unrelated_servers(self):
        executor = MCP_TOOL_EXECUTOR.MCPToolExecutor.__new__(MCP_TOOL_EXECUTOR.MCPToolExecutor)
        executor.log_prefix = "[test]"
        executor.max_candidate_tools = 32
        tools = [
            {"name": "mcp_calendar_list", "mcp_server": "calendar"},
            {"name": "mcp_calendar_detail", "mcp_server": "calendar"},
            {"name": "mcp_filesystem_delete", "mcp_server": "filesystem"},
        ]
        selected = executor._select_candidate_tools(
            tools,
            "列出明天的日历并读取详情",
            ("mcp_calendar_list",),
        )
        self.assertEqual(
            [definition["name"] for definition in selected],
            ["mcp_calendar_list", "mcp_calendar_detail"],
        )

    async def test_observation_is_fed_back_for_follow_up_calls(self):
        executor = MCP_TOOL_EXECUTOR.MCPToolExecutor.__new__(MCP_TOOL_EXECUTOR.MCPToolExecutor)
        executor.log_prefix = "[test]"
        executor.prompt_template = "mcp_tool_executor_prompt"
        executor.max_rounds = 3
        executor.max_calls = 5
        executor.max_candidate_tools = 32
        executor.observation_max_chars = 12000
        executor.llm_model = _FakeLLM()
        executor.mcp_service = _FakeService()

        async def execute_tool_calls(tool_calls, *, access_context=None):
            results = [
                {
                    "type": "tool_result",
                    "tool_name": call.func_name,
                    "content": "evt-1" if call.func_name.endswith("list") else "meeting at 10:00",
                }
                for call in tool_calls
            ]
            return results, [call.func_name for call in tool_calls]

        executor.execute_tool_calls = execute_tool_calls

        results, used_tools, prompt = await executor.execute_from_chat_message(
            target_message="列出明天的日历并读取详情",
            chat_history="",
            sender="tester",
            return_details=True,
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(used_tools, ["mcp_calendar_list", "mcp_calendar_detail"])
        self.assertEqual(len(executor.llm_model.prompts), 3)
        self.assertIn("<MCP_OBSERVATION", executor.llm_model.prompts[1])
        self.assertIn("evt-1", executor.llm_model.prompts[1])
        self.assertIn("meeting at 10:00", executor.llm_model.prompts[2])
        self.assertIn("meeting at 10:00", prompt)


if __name__ == "__main__":
    unittest.main()
