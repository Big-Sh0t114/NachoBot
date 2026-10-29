"""Isolated tests for bounded lifetime of tool and web-search result caches."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load isolated test module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_class_from_source(
    path: Path,
    class_name: str,
    namespace: dict[str, Any],
) -> type:
    """Compile one class body directly, without importing its production package."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    if class_node is None:
        raise ImportError(f"Class {class_name} was not found in {path}")
    isolated_module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            class_node,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(isolated_module)
    module_namespace = dict(namespace)
    exec(compile(isolated_module, str(path), "exec"), module_namespace)
    return module_namespace[class_name]


class _NullLogger:
    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None


_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
_RESULT_CACHE = _load_module(
    "_isolated_result_cache_lifetime", _SOURCE_ROOT / "common" / "result_cache.py"
)
_STORE = _RESULT_CACHE.ResultCacheStore
_MAPPING_VIEW = _RESULT_CACHE.ResultCacheMappingView
_LOGGER = _NullLogger()


async def _unused_async_prompt(*_args, **_kwargs) -> str:
    return "prompt"


_TOOL_EXECUTOR = _load_class_from_source(
    _SOURCE_ROOT / "plugin_system" / "core" / "tool_use.py",
    "ToolExecutor",
    {
        "ResultCacheMappingView": _MAPPING_VIEW,
        "create_result_cache_owner": _RESULT_CACHE.create_result_cache_owner,
        "logger": _LOGGER,
        "time": time,
        "global_config": SimpleNamespace(bot=SimpleNamespace(nickname="test-bot")),
        "global_prompt_manager": SimpleNamespace(
            format_prompt=_unused_async_prompt,
        ),
    },
)
_WEB_SEARCH_MANAGER = _load_class_from_source(
    _SOURCE_ROOT / "chat" / "utils" / "web_search.py",
    "WebSearchManager",
    {
        "ResultCacheMappingView": _MAPPING_VIEW,
        "create_result_cache_owner": _RESULT_CACHE.create_result_cache_owner,
        "logger": _LOGGER,
        "time": time,
    },
)


def _make_tool_executor(
    *, store: Any | None = None, enabled: bool = True, cache_ttl: int = 2, chat_id: str = "chat-a"
) -> Any:
    executor = _TOOL_EXECUTOR.__new__(_TOOL_EXECUTOR)
    executor.chat_id = chat_id
    executor.enable_cache = enabled
    executor.cache_ttl = cache_ttl
    executor.log_prefix = "[test]"
    executor.prompt_template = "tool_executor_prompt"
    store = store or _STORE()
    executor._result_cache = store.create_owner(f"tool_executor:{chat_id}")
    executor.tool_cache = _MAPPING_VIEW(executor._result_cache, "result")
    return executor


def _make_web_manager(
    *, store: Any | None = None, enabled: bool = True, cache_ttl: int = 2, chat_id: str = "chat-a"
) -> Any:
    manager = _WEB_SEARCH_MANAGER.__new__(_WEB_SEARCH_MANAGER)
    manager.chat_id = chat_id
    manager.enable_cache = enabled
    manager.cache_ttl = cache_ttl
    store = store or _STORE()
    manager._result_cache = store.create_owner(f"web_search:{chat_id}")
    manager._cache = _MAPPING_VIEW(manager._result_cache, "content")
    return manager


class ResultCacheStoreTests(unittest.TestCase):
    def test_unique_entries_stay_within_owner_entry_cap(self) -> None:
        store = _STORE()
        owner = store.create_owner("many-unique-results")

        for index in range(_RESULT_CACHE.DEFAULT_RESULT_CACHE_OWNER_MAX_ENTRIES + 20):
            self.assertTrue(owner.set(f"key-{index}", {"value": index}, uses=3))

        self.assertEqual(len(owner), _RESULT_CACHE.DEFAULT_RESULT_CACHE_OWNER_MAX_ENTRIES)
        self.assertFalse(owner.has_key("key-0"))
        self.assertTrue(owner.has_key("key-20"))
        self.assertLessEqual(store.stats["entry_count"], _RESULT_CACHE.DEFAULT_RESULT_CACHE_MAX_ENTRIES)

    def test_global_entry_and_byte_budgets_apply_across_owners(self) -> None:
        store = _STORE(
            owner_max_entries=8,
            owner_max_bytes=2_000,
            max_entries=3,
            max_bytes=1_400,
        )
        owners = [store.create_owner(f"chat-{index}") for index in range(5)]

        for index, owner in enumerate(owners):
            self.assertTrue(owner.set("query", {"result": "x" * 80, "n": index}, uses=5))

        stats = store.stats
        self.assertLessEqual(stats["entry_count"], 3)
        self.assertLessEqual(stats["total_bytes"], 1_400)
        self.assertEqual(stats["entry_count"], sum(len(owner) for owner in owners))

    def test_stale_unused_entries_expire_on_a_later_miss(self) -> None:
        now = [10.0]
        store = _STORE(clock=lambda: now[0])
        owner = store.create_owner("stale-unused", max_age_seconds=4.0)
        for index in range(3):
            self.assertTrue(owner.set(f"old-{index}", {"value": index}, uses=10))

        now[0] += 5.0
        self.assertEqual(owner.get("never-looked-up"), (False, None))
        self.assertEqual(len(owner), 0)
        self.assertEqual(store.stats["entry_count"], 0)

    def test_owner_and_global_lru_order_are_exact(self) -> None:
        store = _STORE(owner_max_entries=3, owner_max_bytes=2_000, max_entries=3, max_bytes=4_000)
        first = store.create_owner("first-chat")
        second = store.create_owner("second-chat")
        first.set("a", "A", uses=5)
        first.set("c", "C", uses=5)
        second.set("b", "B", uses=5)

        self.assertEqual(first.get("a"), (True, "A"))
        first.set("d", "D", uses=5)

        self.assertEqual(first.keys(), ["a", "d"])
        self.assertEqual(second.keys(), ["b"])

        owner_lru_store = _STORE(owner_max_entries=2, owner_max_bytes=2_000, max_entries=4, max_bytes=4_000)
        owner_lru = owner_lru_store.create_owner("owner-lru")
        owner_lru.set("old", "old", uses=5)
        owner_lru.set("new", "new", uses=5)
        self.assertEqual(owner_lru.get("old"), (True, "old"))
        owner_lru.set("added", "added", uses=5)
        self.assertEqual(owner_lru.keys(), ["old", "added"])

    def test_age_key_payload_and_owner_budgets_reject_oversized_data(self) -> None:
        store = _STORE(owner_max_entries=4, owner_max_bytes=256, max_entries=4, max_bytes=1_024)
        owner = store.create_owner("small-owner")
        large_value = {"text": "z" * 1_000}
        large_key = "q" * 1_000

        self.assertFalse(owner.set("large-value", large_value, uses=2))
        self.assertFalse(owner.set(large_key, "small", uses=2))
        self.assertEqual(large_value, {"text": "z" * 1_000})
        self.assertEqual(store.stats["entry_count"], 0)

    def test_unsupported_cyclic_deep_and_nonfinite_values_are_skipped(self) -> None:
        store = _STORE()
        owner = store.create_owner("unsupported-values")
        cycle: list[Any] = []
        cycle.append(cycle)
        deep: Any = "leaf"
        for _ in range(40):
            deep = [deep]

        for key, value in (
            ("object", object()),
            ("cycle", cycle),
            ("deep", deep),
            ("nan", float("nan")),
            ("positive-infinity", float("inf")),
            ("integer-key", {1: "not-json-like"}),
        ):
            self.assertFalse(owner.set(key, value, uses=2), key)
        self.assertEqual(len(owner), 0)

        self.assertTrue(owner.set("replace-me", "cached old value", uses=2))
        self.assertFalse(owner.set("replace-me", object(), uses=2))
        self.assertEqual(owner.get("replace-me"), (False, None))

    def test_values_are_copied_on_set_and_each_get(self) -> None:
        store = _STORE()
        owner = store.create_owner("copy-isolation")
        original = {"items": [1, {"label": "saved"}], "pair": (b"raw", None)}
        self.assertTrue(owner.set("result", original, uses=3))
        original["items"][1]["label"] = "changed-after-set"

        found, first = owner.get("result")
        self.assertTrue(found)
        self.assertEqual(first["items"][1]["label"], "saved")
        first["items"].append("caller mutation")
        first["pair"] = (b"changed",)

        found, second = owner.get("result")
        self.assertTrue(found)
        self.assertEqual(second, {"items": [1, {"label": "saved"}], "pair": (b"raw", None)})
        self.assertEqual(owner.get("result")[0], True)
        self.assertEqual(owner.get("result"), (False, None))

    def test_byte_accounting_uses_builtin_object_sizes(self) -> None:
        store = _STORE()
        owner = store.create_owner("unicode-size")
        text = "😀" + "a" * 1_024
        payload = {"text": text}
        self.assertTrue(owner.set("unicode-key", payload, uses=2))

        minimum = (
            sys.getsizeof(text)
            + sys.getsizeof("text")
            + sys.getsizeof(payload)
            + sys.getsizeof("unicode-key")
            + 512
        )
        self.assertGreaterEqual(store.stats["total_bytes"], minimum)

    def test_cache_namespaces_are_isolated(self) -> None:
        store = _STORE()
        chat_a = store.create_owner("web_search:chat-a")
        chat_b = store.create_owner("web_search:chat-b")
        tool = store.create_owner("tool_executor:chat-a")
        chat_a.set("same-query", "from a", uses=2)
        chat_b.set("same-query", "from b", uses=2)
        tool.set("same-query", "tool result", uses=2)

        self.assertEqual(chat_a.get("same-query"), (True, "from a"))
        self.assertEqual(chat_b.get("same-query"), (True, "from b"))
        self.assertEqual(tool.get("same-query"), (True, "tool result"))


class CacheWrapperCompatibilityTests(unittest.TestCase):
    def test_unique_tool_and_search_results_plateau_and_expire_without_hits(self) -> None:
        now = [1_000.0]
        store = _STORE(clock=lambda: now[0])
        executor = _make_tool_executor(store=store, cache_ttl=3)
        manager = _make_web_manager(store=store, cache_ttl=3)

        for index in range(5_000):
            executor._set_cache(f"tool-{index}", [{"result": index}])
            manager._set_cache(f"query-{index}", f"search-result-{index}")

        owner_limit = _RESULT_CACHE.DEFAULT_RESULT_CACHE_OWNER_MAX_ENTRIES
        self.assertEqual(len(executor.tool_cache), owner_limit)
        self.assertEqual(len(manager._cache), owner_limit)
        self.assertEqual(store.stats["entry_count"], 2 * owner_limit)

        now[0] += 86_401.0
        self.assertIsNone(executor._get_from_cache("unrelated-miss"))
        self.assertEqual(len(executor.tool_cache), 0)
        self.assertEqual(len(manager._cache), 0)
        self.assertEqual(store.stats["entry_count"], 0)

    def test_tool_use_count_status_mapping_and_clear_contract(self) -> None:
        executor = _make_tool_executor(cache_ttl=2)
        expected = [{"tool_name": "search", "content": "cached"}]
        executor._set_cache("tool-key", expected)

        self.assertEqual(executor.tool_cache["tool-key"]["result"], expected)
        self.assertIn("timestamp", executor.tool_cache["tool-key"])
        self.assertEqual(executor._get_from_cache("tool-key"), expected)
        self.assertEqual(executor.get_cache_status()["ttl_distribution"], {1: 1})
        self.assertEqual(executor._get_from_cache("tool-key"), expected)
        self.assertIsNone(executor._get_from_cache("tool-key"))
        self.assertEqual(
            executor.get_cache_status(),
            {"enabled": True, "cache_count": 0, "cache_ttl": 2, "ttl_distribution": {}},
        )

        executor._set_cache("clear-me", expected)
        executor.clear_cache()
        self.assertEqual(len(executor.tool_cache), 0)

    def test_web_search_use_count_and_mapping_shape(self) -> None:
        manager = _make_web_manager(cache_ttl=2)
        manager._set_cache("official query", "cached answer")

        self.assertEqual(
            manager._cache["official query"]["content"],
            "cached answer",
        )
        self.assertEqual(manager._get_cache("official query"), "cached answer")
        self.assertEqual(manager._cache["official query"]["ttl"], 1)
        self.assertEqual(manager._get_cache("official query"), "cached answer")
        self.assertIsNone(manager._get_cache("official query"))
        self.assertNotIn("official query", manager._cache)

    def test_disabled_caches_do_not_read_or_store(self) -> None:
        executor = _make_tool_executor(enabled=False)
        executor._set_cache("tool-key", [{"value": "tool"}])
        self.assertIsNone(executor._get_from_cache("tool-key"))
        self.assertEqual(executor.get_cache_status(), {"enabled": False, "cache_count": 0})

        manager = _make_web_manager(enabled=False)
        manager._set_cache("query", "answer")
        self.assertIsNone(manager._get_cache("query"))
        self.assertEqual(len(manager._cache), 0)

    def test_context_sensitive_tool_execution_bypasses_existing_cache(self) -> None:
        executor = _make_tool_executor(cache_ttl=3)
        executor._set_cache("context-key", [{"tool_name": "old", "content": "old result"}])
        executor._get_tool_definitions = lambda: []

        async def prepare_sensitive_context(_tools: Any) -> dict[str, Any]:
            return {"tool": (object(), SimpleNamespace(cache_sensitive=True))}

        async def generate_response(**_kwargs: Any) -> tuple[str, tuple[str, str, list[Any]]]:
            return "tool decision", ("", "stub-model", [])

        calls = {"execute": 0}

        async def execute_tools(_calls: Any, _contexts: Any) -> tuple[list[dict[str, str]], list[str]]:
            calls["execute"] += 1
            return ([{"tool_name": "fresh", "content": "fresh result"}], ["fresh"])

        executor._prepare_tool_execution_contexts = prepare_sensitive_context
        executor._format_tool_context_supplement = lambda _contexts: ""
        executor._generate_cache_key = lambda *_args: "context-key"
        executor.llm_model = SimpleNamespace(
            model_for_task=SimpleNamespace(model_list=["stub-model"]),
            generate_response_async=generate_response,
        )
        executor.execute_tool_calls = execute_tools

        result, used_tools, _prompt = asyncio.run(
            executor.execute_from_chat_message("target", "history", "sender", return_details=True)
        )

        self.assertEqual(result, [{"tool_name": "fresh", "content": "fresh result"}])
        self.assertEqual(used_tools, ["fresh"])
        self.assertEqual(calls["execute"], 1)
        self.assertEqual(executor.tool_cache["context-key"]["ttl"], 3)
        self.assertEqual(executor.tool_cache["context-key"]["result"][0]["content"], "old result")


if __name__ == "__main__":
    unittest.main()
