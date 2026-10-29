"""Exercise Planner response handling without importing NachoBot modules."""

from __future__ import annotations

import ast
import json
import re
import traceback
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


PLANNER_SOURCES = (
    ("group", "planner_actions/planner.py", "ActionPlanner"),
    ("private", "brain_chat/brain_planner.py", "BrainPlanner"),
)
PLANNER_DIR = Path(__file__).resolve().parents[1] / "src" / "chat"
METHOD_NAMES = {
    "_create_no_reply",
    "_create_reply_fallback",
    "_execute_main_planner",
    "_extract_json_from_markdown",
    "_extract_json_from_raw_content",
    "_filter_raw_json_action_objects",
    "_parse_single_action",
    "_without_silent_actions",
    "find_message_by_id",
}


@dataclass
class _ActionPlannerInfo:
    action_type: str
    reasoning: str = ""
    action_data: dict | None = None
    action_message: object | None = None
    available_actions: dict | None = None
    reply_text: str | None = None


class _SilentLogger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _Message:
    def __init__(self, user_id: str, text: str):
        self.user_info = SimpleNamespace(user_id=user_id, platform="test-platform")
        self.processed_plain_text = text


def _load_planner(source_file: str, class_name: str):
    source_path = PLANNER_DIR / source_file
    parsed = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    source_class = next(
        node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    methods = [
        node
        for node in source_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in METHOD_NAMES
    ]
    helper_functions = [
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name in {"_is_bot_message", "_pick_latest_user_message"}
    ]
    extracted_class = ast.ClassDef(
        name="ExtractedPlanner",
        bases=[],
        keywords=[],
        body=methods,
        decorator_list=[],
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *helper_functions,
            extracted_class,
        ],
        type_ignores=[],
    )
    namespace = {
        "ActionPlannerInfo": _ActionPlannerInfo,
        "SWITCH_CHAT_ACTION": "switch_chat",
        "normalize_switch_action_data": lambda action: dict(action),
        "can_offer_switch_chat": lambda *_args: True,
        "focus_coordinator": object(),
        "global_config": SimpleNamespace(
            bot=SimpleNamespace(qq_account="bot", platform="test-platform"),
            debug=SimpleNamespace(show_prompt=False),
        ),
        "advanced_manager": SimpleNamespace(is_on=lambda _stream: False),
        "get_chat_manager": lambda: SimpleNamespace(get_stream=lambda _chat_id: None),
        "ReqAbortException": type("ReqAbortException", (Exception,), {}),
        "json": json,
        "re": re,
        "repair_json": lambda content: content,
        "traceback": traceback,
        "logger": _SilentLogger(),
    }
    module = ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["ExtractedPlanner"], namespace


class PlannerPlaintextReplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.planners = [
            (kind, *_load_planner(source_file, class_name))
            for kind, source_file, class_name in PLANNER_SOURCES
        ]

    def _run_planner(
        self,
        kind,
        planner_type,
        namespace,
        response,
        message_id_list,
        *,
        allow_no_reply=True,
        available_actions=None,
        error=None,
    ):
        planner = object.__new__(planner_type)
        planner.chat_id = "test-chat"
        planner.log_prefix = "[test-chat]"
        llm = Mock()
        if error is None:
            llm.generate_response_async = AsyncMock(return_value=(response, (None, None, None)))
        else:
            llm.generate_response_async = AsyncMock(side_effect=error)
        planner.planner_llm = llm
        planner.separated_llm = llm

        return self._run_async(
            planner._execute_main_planner(
                prompt="prompt",
                message_id_list=message_id_list,
                filtered_actions=available_actions or {},
                available_actions=available_actions or {},
                loop_start_time=0.0,
                allow_no_reply=allow_no_reply,
            )
        )

    @staticmethod
    def _run_async(awaitable):
        import asyncio

        return asyncio.run(awaitable)

    @staticmethod
    def _fenced_json(action):
        return f"```json\n{json.dumps(action)}\n```"

    def test_plaintext_creates_one_internal_reply_for_latest_eligible_message(self):
        older_message = _Message("user-1", "older")
        latest_message = _Message("user-2", "latest")
        bot_message = _Message("bot", "bot response")
        message_id_list = [("m1", older_message), ("m2", latest_message), ("m3", bot_message)]

        for kind, planner_type, namespace in self.planners:
            with self.subTest(planner=kind):
                actions = self._run_planner(
                    kind,
                    planner_type,
                    namespace,
                    "我会接着回答这个问题。",
                    message_id_list,
                    available_actions={"no_reply": object(), "wait_time": object()},
                )

                self.assertEqual([action.action_type for action in actions], ["reply"])
                self.assertIs(actions[0].action_message, latest_message)
                self.assertFalse(actions[0].reply_text)

    def test_empty_failures_and_structured_non_actions_keep_existing_silent_behavior(self):
        message = _Message("user-1", "hello")
        message_id_list = [("m1", message)]
        responses = (None, "", " \n\t ", '{"action":', '```json\n{"action":\n```', '{"unknown":true}')

        for kind, planner_type, namespace in self.planners:
            for response in responses:
                with self.subTest(planner=kind, response=response):
                    actions = self._run_planner(kind, planner_type, namespace, response, message_id_list)
                    self.assertEqual([action.action_type for action in actions], ["no_reply"])

            with self.subTest(planner=kind, response="request error"):
                actions = self._run_planner(
                    kind,
                    planner_type,
                    namespace,
                    None,
                    message_id_list,
                    error=RuntimeError("request failed"),
                )
                self.assertEqual([action.action_type for action in actions], ["no_reply"])

    def test_explicit_json_actions_keep_their_existing_behavior(self):
        older_message = _Message("user-1", "older")
        latest_message = _Message("user-2", "latest")
        message_id_list = [("m1", older_message), ("m2", latest_message)]
        cases = (
            ({"action": "no_reply"}, "no_reply", None),
            ({"action": "wait_time"}, "wait_time", None),
            ({"action": "switch_chat"}, "switch_chat", None),
            (
                {"action": "reply", "target_message_id": "m1", "text": "explicit reply text"},
                "reply",
                older_message,
            ),
        )

        for kind, planner_type, namespace in self.planners:
            for action_json, expected_type, expected_message in cases:
                with self.subTest(planner=kind, action=action_json["action"]):
                    actions = self._run_planner(
                        kind,
                        planner_type,
                        namespace,
                        self._fenced_json(action_json),
                        message_id_list,
                        available_actions={"wait_time": object()},
                    )
                    self.assertEqual([action.action_type for action in actions], [expected_type])
                    if expected_message is not None:
                        self.assertIs(actions[0].action_message, expected_message)
                    if action_json["action"] == "reply":
                        if kind == "private":
                            self.assertEqual(actions[0].reply_text, "explicit reply text")
                        else:
                            self.assertEqual(actions[0].action_data["text"], "explicit reply text")


if __name__ == "__main__":
    unittest.main()
