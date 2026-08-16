from __future__ import annotations

import importlib.util
import json
import logging
import pathlib
import sys
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "src" / "chat" / "utils" / "capability_router.py"
MODULE_SPEC = importlib.util.spec_from_file_location("capability_router_under_test", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"Unable to load capability router from {MODULE_PATH}")
CAPABILITY_ROUTER = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = CAPABILITY_ROUTER
MODULE_SPEC.loader.exec_module(CAPABILITY_ROUTER)

CapabilityDecision = CAPABILITY_ROUTER.CapabilityDecision
CapabilityRouter = CAPABILITY_ROUTER.CapabilityRouter
execute_mcp_after_decision = CAPABILITY_ROUTER.execute_mcp_after_decision
is_explicit_mcp_request = CAPABILITY_ROUTER.is_explicit_mcp_request
load_json_object = CAPABILITY_ROUTER.load_json_object


class _FakeDecider:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    async def generate_response_async(self, prompt: str):
        self.prompts.append(prompt)
        return self.response, ("", "fake", [])


class _FakeMCPExecutor:
    def __init__(self):
        self.calls: list[dict] = []

    async def execute_from_chat_message(self, **kwargs):
        self.calls.append(kwargs)
        return [{"tool_name": "mcp_calendar_list", "content": "ok"}], [], ""


async def _resolved(value):
    return value


class CapabilityRouterParsingTests(unittest.TestCase):
    def test_load_json_object_accepts_fenced_json(self):
        payload = load_json_object('```json\n{"need_mcp": true}\n```')
        self.assertEqual(payload, {"need_mcp": True})

    def test_plain_mcp_discussion_is_not_an_explicit_execution_request(self):
        self.assertFalse(is_explicit_mcp_request("请解释 MCP 协议是什么"))
        self.assertFalse(is_explicit_mcp_request("如何使用 MCP 工具"))
        self.assertTrue(is_explicit_mcp_request("请使用 MCP 工具查询我的日历"))


class CapabilityRouterDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_decision_respects_allowed_capabilities_and_catalog(self):
        response = json.dumps(
            {
                "need_web_search": True,
                "web_query": "today news",
                "need_mcp": True,
                "mcp_task": "列出明天的日历事件",
                "mcp_tool_names": ["mcp_calendar_list", "invented_tool"],
                "mcp_reason": "private calendar data",
            },
            ensure_ascii=False,
        )
        router = CapabilityRouter(
            "test-chat",
            decider=_FakeDecider(response),
            logger_instance=logging.getLogger("test_capability_router"),
        )

        decision = await router.decide(
            chat_history="",
            sender="tester",
            target="帮我看明天的安排",
            bot_name="bot",
            allow_web_search=False,
            allow_mcp=True,
            mcp_catalog="- mcp_calendar_list: list calendar events",
        )

        self.assertFalse(decision.need_web_search)
        self.assertTrue(decision.need_mcp)
        self.assertEqual(decision.mcp_task, "列出明天的日历事件")
        self.assertEqual(decision.mcp_tool_names, ("mcp_calendar_list",))

    async def test_mcp_is_forced_off_without_an_available_catalog(self):
        router = CapabilityRouter(
            "test-chat",
            decider=_FakeDecider('{"need_mcp": true, "mcp_task": "do it"}'),
            logger_instance=logging.getLogger("test_capability_router"),
        )
        decision = await router.decide(
            chat_history="",
            sender="tester",
            target="do it",
            bot_name="bot",
            allow_web_search=False,
            allow_mcp=True,
            mcp_catalog="",
        )
        self.assertFalse(decision.need_mcp)

    async def test_auto_mcp_disabled_skips_model_for_non_explicit_request(self):
        decider = _FakeDecider('{"need_mcp": true, "mcp_task": "do it"}')
        router = CapabilityRouter(
            "test-chat",
            decider=decider,
            auto_mcp=False,
            logger_instance=logging.getLogger("test_capability_router"),
        )
        decision = await router.decide(
            chat_history="",
            sender="tester",
            target="帮我看看明天的安排",
            bot_name="bot",
            allow_web_search=False,
            allow_mcp=True,
            mcp_catalog="- mcp_calendar_list: list calendar events",
        )
        self.assertFalse(decision.need_mcp)
        self.assertEqual(decider.prompts, [])

    async def test_invalid_model_output_falls_back_to_explicit_mcp_request(self):
        router = CapabilityRouter(
            "test-chat",
            decider=_FakeDecider("not-json"),
            auto_mcp=False,
            logger_instance=logging.getLogger("test_capability_router"),
        )
        decision = await router.decide(
            chat_history="",
            sender="tester",
            target="请使用 MCP 查询我的日历",
            bot_name="bot",
            allow_web_search=False,
            allow_mcp=True,
            mcp_catalog="- mcp_calendar_list: list calendar events",
        )
        self.assertTrue(decision.need_mcp)
        self.assertEqual(decision.mcp_reason, "explicit_mcp_request")

    async def test_mcp_executor_is_not_called_when_route_is_false(self):
        executor = _FakeMCPExecutor()
        result = await execute_mcp_after_decision(
            _resolved(CapabilityDecision()),
            executor,
            chat_history="history",
            sender="tester",
            target="hello",
        )
        self.assertEqual(result, ([], [], ""))
        self.assertEqual(executor.calls, [])

    async def test_mcp_executor_receives_normalized_task_and_candidates(self):
        executor = _FakeMCPExecutor()
        decision = CapabilityDecision(
            need_mcp=True,
            mcp_task="列出明天的日历事件",
            mcp_tool_names=("mcp_calendar_list",),
        )
        await execute_mcp_after_decision(
            _resolved(decision),
            executor,
            chat_history="history",
            sender="tester",
            target="original",
        )
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0]["target_message"], "列出明天的日历事件")
        self.assertEqual(executor.calls[0]["candidate_tool_names"], ("mcp_calendar_list",))


if __name__ == "__main__":
    unittest.main()
