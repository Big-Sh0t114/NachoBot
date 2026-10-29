"""Check current planner variants and registered prompts using isolated source ASTs."""

from __future__ import annotations

import ast
import functools
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from tests.test_prompt_prefix_order import (
    _SegmentedPrompt,
    _capture_registry,
    _prompt_class_namespace,
    _values,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
_PLANNER = "src/chat/planner_actions/planner.py"
_BRAIN = "src/chat/brain_chat/brain_planner.py"
_REPLY_PROMPTS = "src/chat/replyer/prompt/replyer_prompt.py"
_TOOL_USE = "src/plugin_system/core/tool_use.py"


def _class_node(relative_path: str, class_name: str) -> ast.ClassDef:
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)


def _method_node(relative_path: str, class_name: str, method_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    node = _class_node(relative_path, class_name)
    return next(
        child
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name
    )


def _call_keywords(node: ast.AST, function_name: str) -> list[dict[str, ast.AST]]:
    calls = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == function_name
    ]
    return [{keyword.arg: keyword.value for keyword in call.keywords if keyword.arg} for call in calls]


def _load_prompt_map() -> dict[str, Any]:
    prompts: dict[str, Any] = {}
    for relative_path, function_name in (
        (_REPLY_PROMPTS, "init_replyer_prompt"),
        ("src/chat/replyer/prompt/rewrite_prompt.py", "init_rewrite_prompt"),
        (_PLANNER, "init_prompt"),
        (_BRAIN, "init_prompt"),
        (_TOOL_USE, "init_tool_executor_prompt"),
    ):
        prompts.update(_capture_registry(REPO_ROOT, relative_path, function_name))

    tree = ast.parse((REPO_ROOT / "src/chat/utils/web_search.py").read_text(encoding="utf-8"))
    decision_prompt = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DECISION_PROMPT" for target in node.targets)
    )
    namespace = _prompt_class_namespace()
    prompts["web_search_decision"] = namespace["Prompt"](
        ast.literal_eval(decision_prompt),
        "web_search_decision",
        _should_register=False,
        cache_layout=("chat_history", "sender"),
    )
    return prompts


def _turn_values(prompt: Any, name: str, revision: str, *, permission_variant: bool = False) -> dict[str, str]:
    values = _values(prompt.template, revision=revision, session="CHAT_SESSION_A")
    history_field = {
        "planner_prompt": "chat_content_block",
        "brain_planner_prompt": "chat_content_block",
        "brain_integrated_prompt": "dialogue_prompt",
    }[name]
    values[history_field] = f"CHAT_HISTORY_{revision}: {revision} discusses ordinary conversation and next steps.\n"

    for field in ("pending_appointments", "time_block", "actions_before_now_block", "interest"):
        if field in values:
            values[field] = f"{field.upper()}_{revision}"

    current_values = {
        "reply_action_description": "CURRENT_REPLY_ACTION",
        "silent_action_description": "CURRENT_SILENT_ACTION",
        "block_user_action_text": "CURRENT_BLOCK_ACTION",
        "ban_user_action_text": "CURRENT_BAN_ACTION",
        "set_group_title_action_text": "CURRENT_TITLE_ACTION",
        "no_reply_action_description": "CURRENT_NO_REPLY_ACTION",
        "action_options_text": "CURRENT_FILTERED_ACTION_OPTIONS",
        "plan_style": "CURRENT_PLAN_STYLE",
        "reply_style": "CURRENT_PLAN_STYLE",
    }
    if permission_variant:
        current_values.update(
            {
                "silent_action_description": "",
                "block_user_action_text": "",
                "ban_user_action_text": "",
                "no_reply_action_description": "",
                "action_options_text": "CURRENT_ALLOWED_ACTIONS_WITHOUT_RESTRICTED_TOOLS",
                "plan_style": "SILENCE_DISABLED_PLAN_STYLE",
                "reply_style": "SILENCE_DISABLED_PLAN_STYLE",
            }
        )
    for field, value in current_values.items():
        if field in values:
            values[field] = value

    # Keep session labels stable in the three-turn comparison.
    if "chat_context_description" in values:
        values["chat_context_description"] = "CHAT_CONTEXT_FOR_SESSION_A"
    return values


class PlannerCacheLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prompts = _load_prompt_map()

    def test_main_planner_prompts_render_three_user_chunks_with_exact_legacy_text(self):
        for name in ("planner_prompt", "brain_planner_prompt", "brain_integrated_prompt"):
            template = self.prompts[name]
            turns = [
                template.format(**_turn_values(template, name, revision))
                for revision in ("A", "B", "C")
            ]
            with self.subTest(prompt=name):
                for rendered in turns:
                    self.assertIsInstance(rendered, _SegmentedPrompt)
                    self.assertEqual(len(rendered.segments), 3)
                self.assertEqual(turns[0].segments[0], turns[1].segments[0])
                self.assertEqual(turns[1].segments[0], turns[2].segments[0])
                self.assertNotEqual(turns[0].segments[1], turns[1].segments[1])
                self.assertNotEqual(turns[1].segments[1], turns[2].segments[1])
                self.assertNotEqual(turns[0].segments[2], turns[1].segments[2])
                for revision, rendered in zip(("A", "B", "C"), turns, strict=True):
                    values = _turn_values(template, name, revision)
                    self.assertEqual(str(rendered), template.template.format(**values))
                    self.assertIn(f"CHAT_HISTORY_{revision}", rendered.segments[1])

        planner = self.prompts["planner_prompt"].format(
            **_turn_values(self.prompts["planner_prompt"], "planner_prompt", "A")
        )
        self.assertIn("CURRENT_REPLY_ACTION", planner.segments[0])
        self.assertIn("CURRENT_PLAN_STYLE", planner.segments[0])
        self.assertIn("INTEREST_A", planner.segments[2])

        brain = self.prompts["brain_planner_prompt"].format(
            **_turn_values(self.prompts["brain_planner_prompt"], "brain_planner_prompt", "A")
        )
        self.assertIn("CURRENT_NO_REPLY_ACTION", brain.segments[0])
        self.assertIn("CURRENT_FILTERED_ACTION_OPTIONS", brain.segments[0])
        self.assertIn("CURRENT_PLAN_STYLE", brain.segments[0])

        integrated = self.prompts["brain_integrated_prompt"].format(
            **_turn_values(self.prompts["brain_integrated_prompt"], "brain_integrated_prompt", "A")
        )
        self.assertIn("CURRENT_NO_REPLY_ACTION", integrated.segments[0])
        self.assertIn("CURRENT_FILTERED_ACTION_OPTIONS", integrated.segments[0])
        self.assertIn("CURRENT_PLAN_STYLE", integrated.segments[0])
        self.assertIn("ACTIONS_BEFORE_NOW_BLOCK_A", integrated.segments[2])

    def test_every_builtin_history_prompt_declares_its_three_chunk_layout(self):
        layouts = {
            "replyer_prompt": ("background_dialogue_prompt", "focus_handoff_block"),
            "replyer_self_prompt": ("background_dialogue_prompt", "focus_handoff_block"),
            "private_replyer_prompt": ("dialogue_prompt", "focus_handoff_block"),
            "private_replyer_self_prompt": ("dialogue_prompt", "focus_handoff_block"),
            "file_edit_prompt": ("dialogue_prompt", "focus_handoff_block"),
            "brain_integrated_prompt": ("dialogue_prompt", "actions_before_now_block"),
            "default_expressor_prompt": ("chat_info", "mood_state"),
            "planner_prompt": ("chat_content_block", "interest"),
            "brain_planner_prompt": ("chat_content_block", "interest"),
            "tool_executor_prompt": ("chat_history", "sender"),
            "mcp_tool_executor_prompt": ("chat_history", "sender"),
            "web_search_decision": ("chat_history", "sender"),
        }
        for name, (history_field, first_tail_field) in layouts.items():
            prompt = self.prompts[name]
            values = _values(prompt.template)
            rendered = prompt.format(**values)
            with self.subTest(prompt=name):
                self.assertIsInstance(rendered, _SegmentedPrompt)
                self.assertEqual(len(rendered.segments), 3)
                self.assertEqual(str(rendered), prompt.template.format(**values))
                history_index = prompt.template.index("{" + history_field + "}")
                tail_index = prompt.template.index("{" + first_tail_field + "}")
                self.assertLess(history_index, tail_index)

    def test_permission_and_silence_variant_rebuilds_current_prefix_each_turn(self):
        cases = (
            ("planner_prompt", ("CURRENT_SILENT_ACTION", "CURRENT_BLOCK_ACTION", "CURRENT_BAN_ACTION")),
            ("brain_planner_prompt", ("CURRENT_NO_REPLY_ACTION",)),
            ("brain_integrated_prompt", ("CURRENT_NO_REPLY_ACTION",)),
        )
        for name, removed_markers in cases:
            prompt = self.prompts[name]
            allowed = prompt.format(**_turn_values(prompt, name, "A"))
            disabled = prompt.format(**_turn_values(prompt, name, "D", permission_variant=True))
            with self.subTest(prompt=name):
                self.assertNotEqual(allowed.segments[0], disabled.segments[0])
                for marker in removed_markers:
                    self.assertIn(marker, allowed.segments[0])
                    self.assertNotIn(marker, disabled.segments[0])
                self.assertIn("CURRENT_ALLOWED_ACTIONS_WITHOUT_RESTRICTED_TOOLS", disabled.segments[0])
                self.assertIn("SILENCE_DISABLED_PLAN_STYLE", disabled.segments[0])
                self.assertNotIn("CURRENT_PLAN_STYLE", disabled.segments[0])
                self.assertNotIn('"action": "no_reply"', str(disabled))

    async def _run_action_options(self, relative_path: str, class_name: str, template_name: str, actions: dict):
        method = _method_node(relative_path, class_name, "_build_action_options_block")

        class ActionPrompt:
            def format(self, **values):
                parameter_names = [
                    line.strip().split(":", 1)[0]
                    for line in values["action_parameters"].splitlines()
                    if line.strip()
                ]
                return f"<{values['action_name']}:{','.join(parameter_names)}>"

        class PromptManager:
            async def get_prompt_async(self, requested_name):
                self.requested_name = requested_name
                return ActionPrompt()

        namespace = {
            "ActionInfo": Any,
            "Dict": Dict,
            "global_prompt_manager": PromptManager(),
        }
        module = ast.Module(body=[method], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), relative_path, "exec"), namespace)
        result = await namespace["_build_action_options_block"](SimpleNamespace(), actions)
        return result, namespace["global_prompt_manager"].requested_name, template_name

    def test_current_action_catalog_order_is_sorted_by_name_only(self):
        async def run():
            actions_forward = {
                "z_tool": SimpleNamespace(description="Z", action_parameters={}, action_require=[]),
                "a_tool": SimpleNamespace(
                    description="A",
                    action_parameters={"z_param": "last by name", "a_param": "first by name"},
                    action_require=[],
                ),
                "m_tool": SimpleNamespace(description="M", action_parameters={}, action_require=[]),
            }
            actions_reverse = dict(reversed(tuple(actions_forward.items())))
            for path, class_name, prompt_name in (
                (_PLANNER, "ActionPlanner", "action_prompt"),
                (_BRAIN, "BrainPlanner", "brain_action_prompt"),
            ):
                first, first_template, _ = await self._run_action_options(path, class_name, prompt_name, actions_forward)
                second, second_template, _ = await self._run_action_options(path, class_name, prompt_name, actions_reverse)
                self.assertEqual(first, second)
                self.assertEqual(first_template, prompt_name)
                self.assertEqual(second_template, prompt_name)
                self.assertLess(first.index("<a_tool:"), first.index("<m_tool:"))
                self.assertLess(first.index("<m_tool:"), first.index("<z_tool:"))
                self.assertLess(first.index("z_param"), first.index("a_param"))

        import asyncio

        asyncio.run(run())

    def test_reply_prompts_keep_focus_handoff_after_untrusted_history_and_append_safely(self):
        private_template = self.prompts["private_replyer_prompt"]
        values = _values(private_template.template)
        values["dialogue_prompt"] = "ACTORLESS_SYSTEM_EVENT_SENTINEL {identity}"
        values["focus_handoff_block"] = "FOCUS_IDENTITY_ONLY_SENTINEL"
        rendered = private_template.format(**values)
        self.assertIsInstance(rendered, _SegmentedPrompt)
        self.assertIn("ACTORLESS_SYSTEM_EVENT_SENTINEL {identity}", rendered.segments[1])
        self.assertIn("FOCUS_IDENTITY_ONLY_SENTINEL", rendered.segments[2])

        tool_template = self.prompts["tool_executor_prompt"]
        tool_values = _values(tool_template.template)
        tool_values["chat_history"] = "UNTRUSTED_TOOL_HISTORY"
        tool_values["sender"] = "CURRENT_SENDER"
        tool_prompt = tool_template.format(**tool_values)
        before = tool_prompt.segments
        tool_prompt += "\n\nTRUSTED_INTERNAL_TOOL_SUPPLEMENT"
        self.assertEqual(tool_prompt.segments[:-1], before[:-1])
        self.assertTrue(tool_prompt.segments[-1].endswith("TRUSTED_INTERNAL_TOOL_SUPPLEMENT"))
        self.assertIn("UNTRUSTED_TOOL_HISTORY", tool_prompt.segments[1])

        mcp_template = self.prompts["mcp_tool_executor_prompt"]
        mcp_values = _values(mcp_template.template)
        mcp_values["chat_history"] = "UNTRUSTED_MCP_HISTORY"
        mcp_prompt = mcp_template.format(**mcp_values)
        mcp_before = mcp_prompt.segments
        mcp_prompt += "\n\nMCP_LOOP_POLICY"
        mcp_prompt += "\n\nMCP_OBSERVATION"
        self.assertEqual(mcp_prompt.segments[:-1], mcp_before[:-1])
        self.assertTrue(mcp_prompt.segments[-1].endswith("MCP_OBSERVATION"))
        self.assertIn("UNTRUSTED_MCP_HISTORY", mcp_prompt.segments[1])

        mcp_execute = _method_node("src/plugin_system/core/mcp_tool_executor.py", "MCPToolExecutor", "execute_from_chat_message")
        prompt_appends = [
            child
            for child in ast.walk(mcp_execute)
            if isinstance(child, ast.AugAssign)
            and isinstance(child.target, ast.Name)
            and child.target.id == "prompt"
        ]
        self.assertEqual(len(prompt_appends), 2)
        self.assertTrue(any("_format_observation" in ast.unparse(child.value) for child in prompt_appends))

        namespace = _prompt_class_namespace()
        plugin_prompt = namespace["Prompt"]("custom {dialogue_prompt}", "plugin_override", _should_register=False)
        custom = plugin_prompt.format(dialogue_prompt="CUSTOM_HISTORY")
        self.assertIs(type(custom), str)
        self.assertIs(type(f"{rendered}"), str)
        self.assertIs(type(str(rendered)), str)

    def test_chat_owned_request_factories_stay_session_bound_a_to_b_to_a(self):
        class RequestCapture:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        def create_factories(relative_path: str, class_name: str):
            init = _method_node(relative_path, class_name, "__init__")
            target_assignment = next(
                node
                for node in ast.walk(init)
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Attribute) and target.attr == "_express_request_factory"
                    for target in node.targets
                )
            )

            def create_factory(session_id: str):
                return eval(
                    compile(ast.Expression(target_assignment.value), "replyer_factory.py", "eval"),
                    {
                        "partial": functools.partial,
                        "LLMRequest": RequestCapture,
                        "chat_stream": SimpleNamespace(stream_id=session_id),
                    },
                )

            return create_factory

        def assert_a_b_a_factories(relative_path: str, class_name: str):
            create_factory = create_factories(relative_path, class_name)
            requests = [
                create_factory(session)(request_type="replyer")
                for session in ("CHAT_A", "CHAT_B", "CHAT_A")
            ]
            self.assertEqual(
                [request.kwargs["session_id"] for request in requests],
                ["CHAT_A", "CHAT_B", "CHAT_A"],
            )

        assert_a_b_a_factories("src/chat/replyer/group_generator.py", "DefaultReplyer")
        assert_a_b_a_factories("src/chat/replyer/private_generator.py", "PrivateReplyer")

        advanced = _method_node("src/chat/replyer/advanced_generator.py", "AdvancedPrivateReplyer", "express_model")
        cached_call = next(
            child
            for child in ast.walk(advanced)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "cached_replyer_request"
        )
        factory = next(keyword.value for keyword in cached_call.keywords if keyword.arg == "factory")
        self.assertEqual(ast.unparse(factory), "self._express_request_factory")

        request_owners = (
            (_PLANNER, "ActionPlanner", "__init__"),
            (_BRAIN, "BrainPlanner", "__init__"),
            (_BRAIN, "BrainPlanner", "planner_llm"),
            (_TOOL_USE, "ToolExecutor", "__init__"),
            ("src/chat/utils/web_search.py", "WebSearchManager", "__init__"),
            ("src/chat/utils/capability_router.py", "CapabilityRouter", "__init__"),
        )
        for relative_path, class_name, method_name in request_owners:
            node = _method_node(relative_path, class_name, method_name)
            session_values = [keywords["session_id"] for keywords in _call_keywords(node, "LLMRequest") if "session_id" in keywords]
            with self.subTest(owner=(class_name, method_name)):
                self.assertTrue(session_values, relative_path)
                self.assertTrue(any(ast.unparse(value) in {"self.chat_id", "chat_id"} for value in session_values))

        mcp_init = _method_node("src/plugin_system/core/mcp_tool_executor.py", "MCPToolExecutor", "__init__")
        super_calls = [
            child
            for child in ast.walk(mcp_init)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "__init__"
        ]
        self.assertTrue(any(any(keyword.arg == "chat_id" and ast.unparse(keyword.value) == "chat_id" for keyword in call.keywords) for call in super_calls))

    def test_web_search_uses_segmented_prompt_and_capability_router_keeps_json_envelope(self):
        web_tree = ast.parse((REPO_ROOT / "src/chat/utils/web_search.py").read_text(encoding="utf-8"))
        method = next(
            node
            for node in next(node for node in web_tree.body if isinstance(node, ast.ClassDef) and node.name == "WebSearchManager").body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_decide_need_search"
        )
        prompt_calls = [
            child
            for child in ast.walk(method)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "Prompt"
        ]
        self.assertEqual(len(prompt_calls), 1)
        layout_keyword = next(keyword for keyword in prompt_calls[0].keywords if keyword.arg == "cache_layout")
        self.assertEqual(ast.literal_eval(layout_keyword.value), ("chat_history", "sender"))

        router_source = (REPO_ROOT / "src/chat/utils/capability_router.py").read_text(encoding="utf-8")
        router_tree = ast.parse(router_source)
        router_method = next(
            node
            for node in next(node for node in router_tree.body if isinstance(node, ast.ClassDef) and node.name == "CapabilityRouter").body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "decide"
        )
        self.assertFalse(any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "Prompt" for child in ast.walk(router_method)))
        payload_keys = [
            ast.literal_eval(key)
            for child in ast.walk(router_method)
            if isinstance(child, ast.Dict)
            for key in child.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        self.assertTrue({"chat_history", "current_request", "mcp_catalog"}.issubset(payload_keys))


if __name__ == "__main__":
    unittest.main()
