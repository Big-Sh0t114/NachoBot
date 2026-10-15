from __future__ import annotations

import ast
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.config.api_ada_configs import APIProvider, ModelInfo, TaskConfig
from src.llm_models.request_cache import LLMRequestCache
from src.llm_models.utils_model import LLMRequest


def configuration():
    models = {name: ModelInfo(model_identifier=name, name=name, api_provider="test") for name in ("a", "b")}
    provider = APIProvider(name="test", base_url="https://example.invalid", api_key="synthetic-test-key")
    config = SimpleNamespace(get_model_info=models.__getitem__, get_provider=lambda _: provider)
    return config, models, provider


def namespace_request(**kwargs):
    return SimpleNamespace(**kwargs)


class RequestCacheTests(unittest.TestCase):
    def test_existing_selection_penalty_and_inflight_usage_survive_repeated_access(self):
        config, _, _ = configuration()
        task = TaskConfig(model_list=["a", "b"])
        cache = LLMRequestCache()
        def factory(**kwargs):
            return LLMRequest(**kwargs, config=config)
        first = cache.get(model_set=task, request_type="replyer", config=config, factory=factory)
        first.model_usage["a"] = (0, 1, 0, time.time())
        again = cache.get(model_set=task, request_type="replyer", config=config, factory=factory)
        self.assertIs(first, again)
        with patch("src.llm_models.utils_model.client_registry.get_client_class_instance", return_value=object()):
            selected, _, _ = again._select_model()
        self.assertEqual(selected.name, "b", "the normal selector must retain the previous failure penalty")
        self.assertEqual(first.model_usage["b"][2], 1)
        again.release_model_lease("b")
        self.assertEqual(first.model_usage["b"][2], 0)

    def test_task_model_provider_and_configuration_replacement_invalidate(self):
        config, models, provider = configuration()
        task = TaskConfig(model_list=["a", "b"])
        cache = LLMRequestCache()
        factory = namespace_request
        def get():
            return cache.get(model_set=task, request_type="replyer", config=config, factory=factory)
        current = get()
        for mutate in (lambda: setattr(task, "timeout", 12), lambda: task.model_list.reverse(),
                       lambda: models["a"].extra_params.update({"mode": "changed"}),
                       lambda: setattr(provider, "api_key", "rotated-synthetic-key")):
            mutate()
            previous, current = current, get()
            self.assertIsNot(previous, current)
            self.assertIs(current, get())
        replacement = SimpleNamespace(get_model_info=config.get_model_info, get_provider=config.get_provider)
        self.assertIsNot(current, cache.get(model_set=task, request_type="replyer", config=replacement, factory=factory))

    def test_purposes_groups_and_factories_are_independent_and_retention_is_bounded(self):
        config, _, _ = configuration()
        cache = LLMRequestCache(max_entries=2)
        task = TaskConfig(model_list=["a"])
        other_task = TaskConfig(model_list=["a"])
        factory = namespace_request
        def get(group, purpose):
            return cache.get(model_set=group, request_type=purpose, config=config, factory=factory)
        first = get(task, "replyer")
        self.assertIsNot(first, get(task, "tool_use"))
        self.assertIsNot(first, get(other_task, "replyer"))
        self.assertEqual(len(cache._entries), 2)
        self.assertIsNot(first, get(task, "replyer"), "evicted entries are rebuilt")
        def replacement_factory(**kwargs):
            return SimpleNamespace(**kwargs)
        self.assertIsNot(get(task, "replyer"), cache.get(model_set=task, request_type="replyer",
                                                       config=config, factory=replacement_factory))

    def test_unsupported_custom_configuration_keeps_original_creation_path(self):
        config, _, _ = configuration()
        task = SimpleNamespace(model_list=["a"])
        cache = LLMRequestCache()
        factory = namespace_request
        first = cache.get(model_set=task, request_type="custom", config=config, factory=factory)
        self.assertIsNot(first, cache.get(model_set=task, request_type="custom", config=config, factory=factory))
        self.assertFalse(cache._entries)

    def test_all_replyer_properties_reuse_and_keep_their_existing_group_routes(self):
        config, _, _ = configuration()
        base, live, edit, advanced = (TaskConfig(model_list=["a"]) for _ in range(4))
        tasks = SimpleNamespace(replyer=base, realtime_replyer=live, file_edit=edit, advanced_replyer=advanced,
                                get_private_replyer=lambda group: base if group == 0 else live)
        config.model_task_config = tasks
        factory = namespace_request
        paths = [("group_generator.py", "DefaultReplyer", False), ("private_generator.py", "PrivateReplyer", False),
                 ("group_generator.py", "AdvancedGroupReplyer", True), ("advanced_generator.py", "AdvancedPrivateReplyer", True)]
        for filename, class_name, is_advanced in paths:
            with self.subTest(class_name=class_name):
                path = Path(__file__).parents[1] / "src/chat/replyer" / filename
                tree = ast.parse(path.read_text(encoding="utf-8"))
                actual_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
                prop = next(node for node in actual_class.body if isinstance(node, ast.FunctionDef) and node.name == "express_model")
                probe = ast.ClassDef(name="Probe", bases=[], keywords=[], body=[prop], decorator_list=[])
                code = ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[]))
                caps = SimpleNamespace(reply_model_group="")
                global_config = SimpleNamespace(advanced=SimpleNamespace(use_advanced_replyer=True))
                namespace = {"LLMRequest": factory, "model_config": config, "global_config": global_config,
                             "runtime_capabilities_from_stream": lambda _, current_caps=caps: current_caps}
                exec(compile(code, str(path), "exec"), namespace)
                owner = namespace["Probe"]()
                owner.chat_stream = None
                owner.request_type = "advanced_replyer" if is_advanced else "replyer"
                owner.active_private_group = 0
                first = owner.express_model
                self.assertIs(first, owner.express_model)
                self.assertIs(first.model_set, advanced if is_advanced else base)
                if is_advanced:
                    global_config.advanced.use_advanced_replyer = False
                else:
                    caps.reply_model_group = "realtime_replyer"
                    self.assertIs(owner.express_model.model_set, live)
                    caps.reply_model_group = ""
                    self.assertIs(owner.express_model, first)
                owner.request_type = "file_edit"
                self.assertIs(owner.express_model.model_set, edit)
                self.assertIs(owner.express_model, owner.express_model)


class PluginAPIReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_tools_and_message_factory_reuse_state_without_reusing_responses(self):
        from src.plugin_system.apis import llm_api
        config, _, _ = configuration()
        task = TaskConfig(model_list=["a"])
        created = []

        class FakeRequest:
            def __init__(self, **kwargs):
                self.calls = 0
                created.append(self)

            async def generate_response_async(self, prompt, **kwargs):
                self.calls += 1
                return f"{prompt}-{self.calls}", ("", "a", None)

            def _build_tool_options(self, options):
                return options

            def _extract_reasoning(self, content):
                return content, ""

            async def _execute_request(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(content=f"factory-{self.calls}", reasoning_content="", tool_calls=None), SimpleNamespace(name="a")

        with patch.object(llm_api, "model_config", config), patch.object(llm_api, "LLMRequest", FakeRequest), \
                patch.object(llm_api, "_request_cache", LLMRequestCache()):
            first = await llm_api.generate_with_model("first", task)
            second = await llm_api.generate_with_model_with_tools("second", task)
            third = await llm_api.generate_with_model_with_tools_by_message_factory(lambda _: [], task)
            self.assertEqual([first[1], second[1], third[1]], ["first-1", "second-2", "factory-3"])
            self.assertEqual(len(created), 1)
            await llm_api.generate_with_model("other purpose", task, request_type="other")
            self.assertEqual(len(created), 2)
