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
is_explicit_sandbox_edit_request = CAPABILITY_ROUTER.is_explicit_sandbox_edit_request
load_json_object = CAPABILITY_ROUTER.load_json_object
sandbox_veto_reason = CAPABILITY_ROUTER.sandbox_veto_reason
sandbox_trigger_reason = CAPABILITY_ROUTER.sandbox_trigger_reason


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

    def test_deterministic_sandbox_trigger_requires_production_and_artifact_cues(self):
        self.assertEqual(sandbox_trigger_reason("写一个python的hello world"), "deterministic_artifact_trigger")
        self.assertTrue(is_explicit_sandbox_edit_request("帮我创建一个 Python 脚本来打印 Hello World"))
        self.assertTrue(is_explicit_sandbox_edit_request("create a JavaScript script that prints hello"))
        for target in (
            "写一个java hello world",
            "write a C++ hello world",
            "写个 Rust hello world",
            "做个 HTML 网页",
            "请创建 notes.md",
            "请生成 data.csv",
            "create notes.txt",
        ):
            with self.subTest(target=target):
                self.assertTrue(is_explicit_sandbox_edit_request(target))
        self.assertFalse(is_explicit_sandbox_edit_request("解释 Python Hello World 的原理"))
        self.assertFalse(is_explicit_sandbox_edit_request("Python 的 print 怎么用"))
        self.assertFalse(is_explicit_sandbox_edit_request("给我看看一行 Hello World 示例"))
        self.assertFalse(is_explicit_sandbox_edit_request("写一个 Python 代码，只在聊天里贴出来"))
        self.assertFalse(is_explicit_sandbox_edit_request("如何修改文件"))
        self.assertFalse(is_explicit_sandbox_edit_request("如何直接修改文件"))
        self.assertFalse(is_explicit_sandbox_edit_request("怎么创建 Python 文件"))
        self.assertFalse(is_explicit_sandbox_edit_request("怎么直接创建 Python 文件"))
        self.assertTrue(is_explicit_sandbox_edit_request("如何修改文件，请直接创建并发给我"))
        self.assertEqual(sandbox_veto_reason("只在聊天里贴代码"), "inline_only")
        self.assertEqual(sandbox_veto_reason("解释 Python Hello World 的原理"), "educational_request")
        self.assertEqual(sandbox_veto_reason("如何修改文件"), "educational_request")
        self.assertEqual(sandbox_veto_reason("如何直接修改文件"), "educational_request")
        self.assertEqual(sandbox_veto_reason("如何修改文件，请直接创建并发给我"), "")


class CapabilityRouterDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_true_cannot_override_inline_or_educational_veto(self):
        targets = (
            "只在聊天里贴代码，不要生成文件",
            "write code, don't generate a file",
            "解释 Python Hello World 的原理",
            "给我看看一行 Hello World 示例",
            "如何修改文件",
            "如何直接修改文件",
            "怎么创建 Python 文件",
            "怎么直接创建 Python 文件",
        )
        for target in targets:
            with self.subTest(target=target):
                router = CapabilityRouter(
                    "stream-veto",
                    decider=_FakeDecider(
                        '{"need_sandbox_edit": true, "sandbox_task": "create the artifact"}'
                    ),
                    logger_instance=logging.getLogger("test_capability_router"),
                )
                decision = await router.decide(
                    chat_history="",
                    sender="tester",
                    target=target,
                    bot_name="bot",
                    allow_web_search=False,
                    allow_mcp=False,
                    allow_sandbox_edit=True,
                    sandbox_edit_available=True,
                    sandbox_platform="qq",
                    sandbox_group_id="group-veto",
                    sandbox_actor_id="actor-veto",
                    sandbox_source_message_id="message-veto",
                )
                self.assertFalse(decision.need_sandbox_edit)
                self.assertIsNone(decision.sandbox_edit_candidate)

    async def test_question_veto_allows_a_separate_explicit_execution_command(self):
        router = CapabilityRouter(
            "stream-execution-override",
            decider=_FakeDecider(
                '{"need_sandbox_edit": false}'
            ),
            logger_instance=logging.getLogger("test_capability_router"),
        )
        decision = await router.decide(
            chat_history="",
            sender="tester",
            target="如何修改文件，请直接创建并发给我",
            bot_name="bot",
            allow_web_search=False,
            allow_mcp=False,
            allow_sandbox_edit=True,
            sandbox_edit_available=True,
            sandbox_platform="qq",
            sandbox_group_id="group-execution",
            sandbox_actor_id="actor-execution",
            sandbox_source_message_id="message-execution",
        )
        self.assertTrue(decision.need_sandbox_edit)
        self.assertIsNotNone(decision.sandbox_edit_candidate)

    async def test_model_false_cannot_suppress_python_artifact_candidate(self):
        router = CapabilityRouter(
            "stream-artifact",
            decider=_FakeDecider('{"need_sandbox_edit": false}'),
            logger_instance=logging.getLogger("test_capability_router"),
        )
        decision = await router.decide(
            chat_history="",
            sender="tester",
            target="写一个python的hello world",
            bot_name="bot",
            allow_web_search=False,
            allow_mcp=False,
            allow_sandbox_edit=True,
            sandbox_edit_available=True,
            sandbox_platform="qq",
            sandbox_group_id="group-1",
            sandbox_actor_id="actor-1",
            sandbox_source_message_id="message-1",
        )
        self.assertTrue(decision.need_sandbox_edit)
        self.assertIsNotNone(decision.sandbox_edit_candidate)
        self.assertTrue(decision.sandbox_task)
        self.assertEqual(decision.sandbox_edit_candidate.query, "写一个python的hello world")

    async def test_representative_chinese_and_english_artifact_requests_route(self):
        cases = (
            "帮我创建一个 Python 脚本来打印 Hello World",
            "create a JavaScript script that prints hello",
            "请创建 notes.md",
            "请生成 data.csv",
            "create notes.txt",
        )
        for index, target in enumerate(cases):
            with self.subTest(target=target):
                router = CapabilityRouter(
                    f"stream-{index}",
                    decider=_FakeDecider('{"need_sandbox_edit": false}'),
                    logger_instance=logging.getLogger("test_capability_router"),
                )
                decision = await router.decide(
                    chat_history="",
                    sender="tester",
                    target=target,
                    bot_name="bot",
                    allow_web_search=False,
                    allow_mcp=False,
                    allow_sandbox_edit=True,
                    sandbox_edit_available=True,
                    sandbox_platform="qq",
                    sandbox_group_id="group-1",
                    sandbox_actor_id="actor-1",
                    sandbox_source_message_id=f"message-{index}",
                )
                self.assertTrue(decision.need_sandbox_edit)
                self.assertIsNotNone(decision.sandbox_edit_candidate)

    async def test_explicit_file_request_survives_invalid_or_missing_model_output(self):
        for decider in (_FakeDecider("not-json"), None):
            with self.subTest(decider=type(decider).__name__ if decider else "none"):
                router = CapabilityRouter(
                    "stream-file",
                    decider=decider,
                    logger_instance=logging.getLogger("test_capability_router"),
                )
                decision = await router.decide(
                    chat_history="",
                    sender="tester",
                    target="请修改上传的文件",
                    bot_name="bot",
                    allow_web_search=False,
                    allow_mcp=False,
                    allow_sandbox_edit=True,
                    sandbox_edit_available=True,
                    sandbox_platform="qq",
                    sandbox_group_id="group-1",
                    sandbox_actor_id="actor-1",
                    sandbox_source_message_id="message-file",
                )
                self.assertTrue(decision.need_sandbox_edit)
                self.assertIsNotNone(decision.sandbox_edit_candidate)

    async def test_explanations_and_inline_only_requests_stay_out_of_sandbox(self):
        targets = (
            "解释 Python Hello World 的原理",
            "Python 的 print 怎么用",
            "给我看看一行 Hello World 示例",
            "写一个 Python 代码，只在聊天里贴出来",
        )
        for target in targets:
            with self.subTest(target=target):
                router = CapabilityRouter(
                    "stream-ordinary",
                    decider=_FakeDecider('{"need_sandbox_edit": false}'),
                    logger_instance=logging.getLogger("test_capability_router"),
                )
                decision = await router.decide(
                    chat_history="",
                    sender="tester",
                    target=target,
                    bot_name="bot",
                    allow_web_search=False,
                    allow_mcp=False,
                    allow_sandbox_edit=True,
                    sandbox_edit_available=True,
                    sandbox_platform="qq",
                    sandbox_group_id="group-1",
                    sandbox_actor_id="actor-1",
                    sandbox_source_message_id="message-ordinary",
                )
                self.assertFalse(decision.need_sandbox_edit)
                self.assertIsNone(decision.sandbox_edit_candidate)

    async def test_sandbox_unavailable_or_missing_identity_stays_false(self):
        for available, platform, actor_id, message_id in (
            (False, "qq", "actor-1", "message-1"),
            (True, "", "actor-1", "message-1"),
            (True, "qq", "", "message-1"),
            (True, "qq", "actor-1", ""),
        ):
            with self.subTest(available=available, platform=platform, actor_id=actor_id, message_id=message_id):
                router = CapabilityRouter(
                    "stream-unavailable",
                    decider=_FakeDecider('{"need_sandbox_edit": false}'),
                    logger_instance=logging.getLogger("test_capability_router"),
                )
                decision = await router.decide(
                    chat_history="",
                    sender="tester",
                    target="写一个python的hello world",
                    bot_name="bot",
                    allow_web_search=False,
                    allow_mcp=False,
                    allow_sandbox_edit=True,
                    sandbox_edit_available=available,
                    sandbox_platform=platform,
                    sandbox_group_id="group-1",
                    sandbox_actor_id=actor_id,
                    sandbox_source_message_id=message_id,
                )
                self.assertFalse(decision.need_sandbox_edit)
                self.assertIsNone(decision.sandbox_edit_candidate)

    async def test_sandbox_candidate_is_call_local_and_skips_when_unavailable(self):
        response = json.dumps({"need_sandbox_edit": True, "sandbox_task": "修改上传的配置文件"}, ensure_ascii=False)
        router = CapabilityRouter(
            "stream-1",
            decider=_FakeDecider(response),
            logger_instance=logging.getLogger("test_capability_router"),
        )
        decision = await router.decide(
            chat_history="",
            sender="tester",
            target="请修改上传的配置文件",
            bot_name="bot",
            allow_web_search=False,
            allow_mcp=False,
            allow_sandbox_edit=True,
            sandbox_edit_available=True,
            sandbox_platform="qq",
            sandbox_group_id="123",
            sandbox_actor_id="42",
            sandbox_source_message_id="message-1",
        )
        self.assertTrue(decision.need_sandbox_edit)
        self.assertEqual(decision.sandbox_edit_candidate.actor_id, "42")
        self.assertEqual(decision.sandbox_candidate, decision.sandbox_edit_candidate)

        unavailable = await router.decide(
            chat_history="",
            sender="tester",
            target="请修改上传的配置文件",
            bot_name="bot",
            allow_web_search=False,
            allow_mcp=False,
            allow_sandbox_edit=True,
            sandbox_edit_available=False,
            sandbox_platform="qq",
            sandbox_group_id="123",
            sandbox_actor_id="42",
            sandbox_source_message_id="message-1",
        )
        self.assertFalse(unavailable.need_sandbox_edit)
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
