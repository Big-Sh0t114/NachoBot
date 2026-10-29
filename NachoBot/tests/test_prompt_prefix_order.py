"""Render real prompt templates from AST-isolated source without package imports."""

from __future__ import annotations

import ast
import collections
import json
import logging
import re
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Mapping, Optional, Tuple, Union


REPO_ROOT = Path(__file__).resolve().parents[1]
_FIELD_PATTERN = re.compile(r"(?<!\{)\{([A-Za-z_]\w*)\}(?!\})")
_HISTORY_FIELDS = {
    "background_dialogue_prompt",
    "core_dialogue_prompt",
    "dialogue_prompt",
    "chat_info",
    "chat_history",
    "chat_content_block",
}
_SESSION_FIELDS = {
    "latest_session",
    "sender_name",
    "chat_context_description",
    "chat_target",
    "chat_target_2",
}
_CORE_TEMPLATES = (
    "replyer_prompt",
    "replyer_self_prompt",
    "private_replyer_prompt",
    "private_replyer_self_prompt",
    "file_edit_prompt",
    "brain_integrated_prompt",
)
_EXPECTED_FIELDS = {
    "action_prompt": "action_name action_name action_description action_require action_parameters",
    "brain_action_prompt": "action_name action_name action_description action_require action_parameters",
    "brain_integrated_prompt": (
        "identity sender_name dialogue_prompt no_reply_action_description action_options_text reply_style "
        "actions_before_now_block knowledge_prompt memory_retrieval relation_info_block tool_info_block "
        "extra_info_block expression_habits_block reply_target_block keywords_reaction_prompt moderation_prompt time_block"
    ),
    "brain_planner_prompt": (
        "name_block chat_context_description chat_content_block interest no_reply_action_description "
        "action_options_text pending_appointments plan_style time_block actions_before_now_block moderation_prompt"
    ),
    "chat_target_group1": "latest_session latest_session",
    "chat_target_group2": "latest_session",
    "chat_target_private1": "sender_name",
    "chat_target_private2": "sender_name",
    "default_expressor_prompt": (
        "identity reply_style chat_target chat_info mood_state chat_target_2 reply_target_block raw_reply "
        "reason keywords_reaction_prompt expression_habits_block moderation_prompt time_block"
    ),
    "file_edit_prompt": (
        "identity reply_style dialogue_prompt focus_handoff_block knowledge_prompt memory_retrieval tool_info_block "
        "expression_habits_block reply_target_block keywords_reaction_prompt moderation_prompt time_block"
    ),
    "mcp_tool_executor_prompt": "bot_name chat_history sender target_message time_now",
    "planner_prompt": (
        "name_block chat_context_description chat_content_block interest reply_action_description silent_action_description "
        "block_user_action_text ban_user_action_text set_group_title_action_text action_options_text pending_appointments "
        "plan_style time_block actions_before_now_block moderation_prompt"
    ),
    "private_replyer_prompt": (
        "identity reply_style sender_name dialogue_prompt focus_handoff_block knowledge_prompt memory_retrieval "
        "mid_term_memory_block relation_info_block tool_info_block extra_info_block expression_habits_block "
        "reply_target_block keywords_reaction_prompt moderation_prompt time_block"
    ),
    "private_replyer_self_prompt": (
        "identity reply_style sender_name dialogue_prompt focus_handoff_block knowledge_prompt memory_retrieval "
        "mid_term_memory_block relation_info_block tool_info_block extra_info_block expression_habits_block "
        "target target reason keywords_reaction_prompt moderation_prompt time_block"
    ),
    "replyer_prompt": (
        "identity reply_style latest_session latest_session latest_session background_dialogue_prompt core_dialogue_prompt "
        "focus_handoff_block knowledge_prompt memory_retrieval mid_term_memory_block relation_info_block tool_info_block "
        "extra_info_block expression_habits_block reply_target_block keywords_reaction_prompt moderation_prompt time_block"
    ),
    "replyer_self_prompt": (
        "identity reply_style latest_session latest_session background_dialogue_prompt focus_handoff_block knowledge_prompt "
        "memory_retrieval mid_term_memory_block relation_info_block tool_info_block extra_info_block expression_habits_block "
        "target target reason keywords_reaction_prompt moderation_prompt time_block"
    ),
    "tool_executor_prompt": "bot_name chat_history sender target_message time_now",
    "web_search_decision": "bot_name chat_history sender target_message time_now",
}
_REGISTRIES = (
    ("src/chat/replyer/prompt/replyer_prompt.py", "init_replyer_prompt"),
    ("src/chat/replyer/prompt/rewrite_prompt.py", "init_rewrite_prompt"),
    ("src/chat/planner_actions/planner.py", "init_prompt"),
    ("src/chat/brain_chat/brain_planner.py", "init_prompt"),
    ("src/plugin_system/core/tool_use.py", "init_tool_executor_prompt"),
)


class _PromptManager:
    class _Context:
        _current_context = None

    def __init__(self):
        self._context = self._Context()
        self.prompts = {}

    def register(self, prompt):
        self.prompts[prompt.name] = prompt


def _prompt_class_namespace() -> dict[str, Any]:
    prompt_tree = ast.parse((REPO_ROOT / "src/chat/utils/prompt_builder.py").read_text(encoding="utf-8"))
    prompt_class = next(
        node for node in prompt_tree.body if isinstance(node, ast.ClassDef) and node.name == "Prompt"
    )
    namespace = {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Union": Union,
        "re": re,
        "global_prompt_manager": _PromptManager(),
    }
    module = ast.Module(body=[prompt_class], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "prompt_builder_ast.py", "exec"), namespace)
    return namespace


def _capture_registry(root: Path, relative_path: str, function_name: str):
    namespace = _prompt_class_namespace()
    tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), relative_path, "exec"), namespace)
    namespace[function_name]()
    return namespace["global_prompt_manager"].prompts


def _all_templates(root: Path) -> dict[str, str]:
    templates: dict[str, str] = {}
    for relative_path, function_name in _REGISTRIES:
        for name, prompt in _capture_registry(root, relative_path, function_name).items():
            if name in templates:
                if templates[name] != prompt.template:
                    raise AssertionError(f"Duplicate prompt target changed in {relative_path}: {name}")
            templates[name] = prompt.template
    web_search_tree = ast.parse(
        (root / "src/chat/utils/web_search.py").read_text(encoding="utf-8")
    )
    decision = next(
        node.value
        for node in web_search_tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DECISION_PROMPT" for target in node.targets)
    )
    templates["web_search_decision"] = ast.literal_eval(decision)
    return templates


def _placeholder_counts(template: str) -> collections.Counter[str]:
    return collections.Counter(_FIELD_PATTERN.findall(template))


def _values(template: str, *, revision: str = "A", session: str = "SESSION_A") -> dict[str, str]:
    values = {
        field: f"[[ROUND_{revision}_{field}]]"
        for field in _placeholder_counts(template)
    }
    for field in _HISTORY_FIELDS.intersection(values):
        values[field] = f"[[HISTORY_{field}_A]]\n"
    for field in _SESSION_FIELDS.intersection(values):
        values[field] = f"[[{session}_{field}]]"
    for field in ("identity", "name_block", "reply_style", "bot_name"):
        if field in values:
            values[field] = f"FIXED_{field}"
    return values


def _render(template: str, values: dict[str, str]) -> str:
    prompt = _prompt_class_namespace()["Prompt"](template, "isolated", _should_register=False)
    return prompt.format(**values)


def _common_prefix_length(left: str, right: str) -> int:
    for index, (left_char, right_char) in enumerate(zip(left, right, strict=True)):
        if left_char != right_char:
            return index
    return min(len(left), len(right))


class PromptPrefixOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.after = _all_templates(REPO_ROOT)

    def test_registered_template_names_and_core_prompt_contract_are_preserved(self):
        expected_names = {
            *_CORE_TEMPLATES,
            "chat_target_group1",
            "chat_target_private1",
            "chat_target_group2",
            "chat_target_private2",
            "default_expressor_prompt",
            "planner_prompt",
            "action_prompt",
            "brain_planner_prompt",
            "brain_action_prompt",
            "tool_executor_prompt",
            "mcp_tool_executor_prompt",
            "web_search_decision",
        }
        self.assertEqual(set(self.after), expected_names)
        self.assertEqual(set(self.after), set(_EXPECTED_FIELDS))
        for name, fields in _EXPECTED_FIELDS.items():
            with self.subTest(prompt=name):
                self.assertEqual(_placeholder_counts(self.after[name]), collections.Counter(fields.split()))
        for name in _CORE_TEMPLATES:
            with self.subTest(prompt=name):
                self.assertIn("{identity}", self.after[name])
                self.assertIn("{reply_style}", self.after[name])
        self.assertIn("{chat_info}", self.after["default_expressor_prompt"])
        self.assertIn("{pending_appointments}", self.after["planner_prompt"])
        self.assertIn("{pending_appointments}", self.after["brain_planner_prompt"])
        self.assertIn('"action": "reply"', self.after["brain_integrated_prompt"])
        self.assertIn("MCP_TASK_COMPLETE", self.after["mcp_tool_executor_prompt"])

    def test_actual_prompt_formatting_keeps_dynamic_fields_after_fixed_history_prefix(self):
        dynamic_fields = {
            "focus_handoff_block",
            "time_block",
            "pending_appointments",
            "action_options_text",
            "no_reply_action_description",
            "reply_action_description",
            "silent_action_description",
            "block_user_action_text",
            "ban_user_action_text",
            "set_group_title_action_text",
            "plan_style",
            "actions_before_now_block",
            "target",
            "reason",
            "keywords_reaction_prompt",
            "knowledge_prompt",
            "memory_retrieval",
            "mid_term_memory_block",
            "relation_info_block",
            "tool_info_block",
            "extra_info_block",
            "expression_habits_block",
            "moderation_prompt",
            "mood_state",
            "raw_reply",
            "reply_target_block",
            "current_request",
            "sender",
            "target_message",
            "time_now",
        }
        required_semantic_markers = (
            ("replyer_prompt", "FIXED_identity", "FIXED_reply_style", "HISTORY_background_dialogue_prompt_A"),
            ("replyer_self_prompt", "FIXED_identity", "FIXED_reply_style", "HISTORY_background_dialogue_prompt_A"),
            ("private_replyer_prompt", "FIXED_identity", "FIXED_reply_style", "HISTORY_dialogue_prompt_A"),
            ("private_replyer_self_prompt", "FIXED_identity", "FIXED_reply_style", "HISTORY_dialogue_prompt_A"),
            ("file_edit_prompt", "FIXED_identity", "FIXED_reply_style", "HISTORY_dialogue_prompt_A"),
            ("brain_integrated_prompt", "FIXED_identity", "HISTORY_dialogue_prompt_A"),
            ("default_expressor_prompt", "FIXED_identity", "FIXED_reply_style", "HISTORY_chat_info_A"),
            ("planner_prompt", "FIXED_name_block", "HISTORY_chat_content_block_A"),
            ("brain_planner_prompt", "FIXED_name_block", "HISTORY_chat_content_block_A"),
            ("tool_executor_prompt", "HISTORY_chat_history_A"),
            ("mcp_tool_executor_prompt", "HISTORY_chat_history_A"),
            ("web_search_decision", "HISTORY_chat_history_A"),
        )
        for name, *markers in required_semantic_markers:
            template = self.after[name]
            counts = _placeholder_counts(template)
            first_values = _values(template, revision="A")
            second_values = _values(template, revision="B")
            for field in dynamic_fields.intersection(counts):
                first_values[field] = f"[[DYNAMIC_A_{field}]]"
                second_values[field] = f"[[DYNAMIC_B_{field}]]"
            if name == "brain_integrated_prompt":
                first_values["reply_style"] = "[[PLAN_STYLE_A]]"
                second_values["reply_style"] = "[[PLAN_STYLE_B]]"
            first = _render(template, first_values)
            second = _render(template, second_values)

            for marker in markers:
                self.assertIn(marker, first)
            for field, count in counts.items():
                self.assertEqual(first.count(first_values[field]), count, (name, field, count))

            history_names = [field for field in counts if field in _HISTORY_FIELDS]
            self.assertTrue(history_names, name)
            history_field = history_names[-1]
            history_marker = first_values[history_field]
            history_end = first.index(history_marker) + len(history_marker)
            self.assertEqual(first[:history_end], second[:history_end], name)
            self.assertGreaterEqual(_common_prefix_length(first, second), history_end, name)

            for field in dynamic_fields.intersection(counts):
                self.assertGreaterEqual(first.index(first_values[field]), history_end, (name, field))

            recall_fields = {
                "knowledge_prompt",
                "memory_retrieval",
                "mid_term_memory_block",
                "relation_info_block",
                "tool_info_block",
                "extra_info_block",
            }.intersection(counts)
            target_fields = {"reply_target_block", "target", "raw_reply", "reason"}.intersection(counts)
            if recall_fields and target_fields:
                self.assertGreater(
                    min(first.index(first_values[field]) for field in target_fields),
                    max(first.index(first_values[field]) for field in recall_fields),
                    name,
                )

            appended_values = dict(first_values)
            appended_values[history_field] += "HISTORY_APPENDED\n"
            appended = _render(template, appended_values)
            self.assertTrue(appended.startswith(first[:history_end]), name)
            self.assertIn("HISTORY_APPENDED", appended)
            if name == "brain_integrated_prompt":
                self.assertIn("[[PLAN_STYLE_A]]", first)
                self.assertIn("[[PLAN_STYLE_B]]", second)

    def test_session_labels_are_per_session_and_a_b_a_renders_are_deterministic(self):
        for name, template in self.after.items():
            session_fields = _SESSION_FIELDS.intersection(_placeholder_counts(template))
            if not session_fields:
                continue
            values_a = _values(template, session="SESSION_A")
            values_b = _values(template, session="SESSION_B")
            prompt_a1 = _render(template, values_a)
            prompt_b = _render(template, values_b)
            prompt_a2 = _render(template, values_a)
            with self.subTest(prompt=name):
                self.assertEqual(prompt_a1, prompt_a2)
                self.assertNotEqual(prompt_a1, prompt_b)
                self.assertTrue(any(f"SESSION_A_{field}" in prompt_a1 for field in session_fields))
                self.assertTrue(any(f"SESSION_B_{field}" in prompt_b for field in session_fields))
                first_label = min(prompt_a1.index(f"[[SESSION_A_{field}]]") for field in session_fields)
                self.assertEqual(prompt_a1[:first_label], prompt_b[:first_label])

    def test_moderation_focus_disabled_action_and_json_contract_markers_render(self):
        values = {
            "identity": "BOT_IDENTITY",
            "reply_style": "FIXED_STYLE",
            "focus_handoff_block": "<FOCUS_HANDOFF_IDENTITY_ONLY>",
            "moderation_prompt": "<MODERATION_RULES>",
        }
        for name in ("replyer_prompt", "private_replyer_prompt", "brain_integrated_prompt"):
            template = self.after[name]
            args = _placeholder_counts(template)
            rendered = _render(template, {**_values(template), **{k: v for k, v in values.items() if k in args}})
            if "focus_handoff_block" in args:
                self.assertIn("<FOCUS_HANDOFF_IDENTITY_ONLY>", rendered)
            self.assertIn("<MODERATION_RULES>", rendered)
        private_values = _values(self.after["private_replyer_prompt"])
        private_values["dialogue_prompt"] = "ACTORLESS_SYSTEM_EVENT_SENTINEL"
        self.assertIn("ACTORLESS_SYSTEM_EVENT_SENTINEL", _render(self.after["private_replyer_prompt"], private_values))

        for name in ("brain_integrated_prompt", "planner_prompt", "brain_planner_prompt"):
            template = self.after[name]
            args = _placeholder_counts(template)
            empty_disabled_actions = {
                field: ""
                for field in (
                    "no_reply_action_description",
                    "silent_action_description",
                    "action_options_text",
                )
                if field in args
            }
            rendered = _render(
                template,
                {
                    **_values(template),
                    **{k: v for k, v in values.items() if k in args},
                    **empty_disabled_actions,
                },
            )
            self.assertIn('"action":', rendered)
            self.assertNotIn('"action": "no_reply"', rendered)
            self.assertNotIn('"action": "no_reply_until_call"', rendered)


def _isolated_router_namespace():
    source = REPO_ROOT / "src/chat/utils/capability_router.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if not isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    namespace = {
        "json": json,
        "logging": logging,
        "re": re,
        "time": time,
        "dataclass": dataclass,
        "Any": Any,
        "Awaitable": Awaitable,
        "Dict": Dict,
        "List": List,
        "Mapping": Mapping,
        "Optional": Optional,
        "Tuple": Tuple,
        "SandboxEditCandidate": object,
        "sanitize_file_edit_query": lambda value: value,
    }
    module = ast.Module(body=body, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace


class _MockDecider:
    def __init__(self):
        self.prompt = None
        self.system_prompt = None

    async def generate_response_async(self, prompt, *, system_prompt):
        self.prompt = prompt
        self.system_prompt = system_prompt
        return json.dumps(
            {
                "need_web_search": False,
                "need_mcp": False,
                "need_sandbox_edit": False,
            }
        ), None


class CapabilityRouterPromptOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_decider_receives_reordered_json_envelope_without_payload_changes(self):
        current_namespace = _isolated_router_namespace()
        current_router = current_namespace["CapabilityRouter"]
        current_decider = _MockDecider()
        current = current_router("test", decider=current_decider, auto_mcp=True)

        args = {
            "chat_history": "Earlier discussion: keep using the browser.",
            "sender": "SENDER_SENTINEL",
            "target": "CURRENT_REQUEST_SENTINEL",
            "bot_name": "BOT_NAME_SENTINEL",
            "allow_web_search": True,
            "allow_mcp": True,
            "mcp_catalog": '[{"name":"tool_a","description":"catalog sentinel"}]',
            "allow_sandbox_edit": False,
            "sandbox_edit_available": False,
        }
        await current.decide(**args)

        current_payload = json.loads(current_decider.prompt)
        self.assertEqual(list(current_payload), [
            "bot_name",
            "mcp_catalog",
            "sandbox_edit_available",
            "chat_history",
            "current_request",
            "time_now",
        ])
        self.assertEqual(current_payload["current_request"]["text"], args["target"])
        self.assertEqual(current_payload["current_request"]["sender"], args["sender"])
        self.assertEqual(current_payload["mcp_catalog"], args["mcp_catalog"])
        self.assertEqual(current_payload["chat_history"], args["chat_history"])
        self.assertEqual(current_decider.system_prompt, current_namespace["CAPABILITY_DECISION_SYSTEM_PROMPT"])
        self.assertRegex(current_payload["time_now"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
