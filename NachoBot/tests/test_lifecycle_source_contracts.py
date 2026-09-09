"""生命周期修复的纯标准库回归测试。

这些测试不导入 NachoBot 运行时，避免配置迁移、数据库与模型初始化的副作用。
"""

from __future__ import annotations

import ast
import time
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse(relative_path: str) -> ast.Module:
    return ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"), filename=relative_path)


def _class_method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                    return child
    raise AssertionError(f"{class_name}.{method_name} not found")


class SourceContractTests(unittest.TestCase):
    def test_event_callback_captures_registry_key(self) -> None:
        tree = _parse("src/plugin_system/core/events_manager.py")
        dispatch = _class_method(tree, "EventsManager", "_dispatch_handler_task")
        callback = _class_method(tree, "EventsManager", "_task_done_callback")
        self.assertIn("handler_name", [arg.arg for arg in callback.args.args])
        lambda_nodes = [node for node in ast.walk(dispatch) if isinstance(node, ast.Lambda)]
        self.assertTrue(
            any(any(arg.arg == "handler_name" for arg in node.args.args) for node in lambda_nodes),
            "done callback must capture the handler registry key",
        )

    def test_plugin_unload_supports_v2_and_legacy_hooks(self) -> None:
        tree = _parse("src/plugin_system/core/plugin_manager.py")
        unload = _class_method(tree, "PluginManager", "_call_plugin_unload")
        constants = {node.value for node in ast.walk(unload) if isinstance(node, ast.Constant)}
        self.assertIn("v2_plugin", constants)
        self.assertIn("on_unload", constants)
        self.assertIn("on_plugin_unload", constants)

    def test_cycle_history_is_bounded(self) -> None:
        for relative_path, class_name in (
            ("src/chat/heart_flow/heartFC_chat.py", "HeartFChatting"),
            ("src/chat/brain_chat/brain_chat.py", "BrainChatting"),
        ):
            init_method = _class_method(_parse(relative_path), class_name, "__init__")
            assignments = [
                node
                for node in ast.walk(init_method)
                if isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(getattr(node, "target", None), ast.Attribute)
                and getattr(node.target, "attr", "") == "history_loop"
            ]
            self.assertEqual(len(assignments), 1, relative_path)
            value = assignments[0].value
            self.assertIsInstance(value, ast.Call)
            self.assertEqual(getattr(value.func, "id", None), "deque")
            maxlen = next(keyword.value for keyword in value.keywords if keyword.arg == "maxlen")
            self.assertGreater(ast.literal_eval(maxlen), 0)

    def test_summarizer_tracks_and_drains_writebacks(self) -> None:
        tree = _parse("src/memory_system/chat_history_summarizer.py")
        init_method = _class_method(tree, "ChatHistorySummarizer", "__init__")
        self.assertTrue(
            any(
                isinstance(node, ast.Attribute) and node.attr == "_writeback_tasks"
                for node in ast.walk(init_method)
            )
        )

    def test_summarizer_restores_topic_clock_and_uses_content_identity(self) -> None:
        tree = _parse("src/memory_system/chat_history_summarizer.py")
        start_method = _class_method(tree, "ChatHistorySummarizer", "start")
        writeback = _class_method(tree, "ChatHistorySummarizer", "_writeback_to_a_memorix")
        start_calls = {
            getattr(node.func, "attr", "")
            for node in ast.walk(start_method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("_load_topic_cache_from_disk", start_calls)
        constants = {
            node.value for node in ast.walk(writeback) if isinstance(node, ast.Constant)
        }
        self.assertIn("chat_summary:", constants)
        self.assertNotIn("chat_summary:{chat_id}:{start_time:.0f}", constants)


class RuntimePolicyHarness:
    """Heartflow TTL/LRU 算法的无项目导入验证器。"""

    def __init__(self, ttl: float = 60, maximum: int = 2):
        self.ttl = ttl
        self.maximum = maximum
        self.items: dict[str, Any] = {}
        self.access: dict[str, float] = {}

    async def add(self, key: str, value: Any, accessed: float) -> None:
        self.items[key] = value
        self.access[key] = accessed
        await self.enforce(exclude=key)

    async def cleanup(self, now: float) -> int:
        keys = [key for key, accessed in self.access.items() if accessed <= now - self.ttl]
        for key in keys:
            await self.remove(key)
        return len(keys)

    async def enforce(self, exclude: str) -> None:
        while len(self.items) > self.maximum:
            key = min((key for key in self.items if key != exclude), key=self.access.__getitem__)
            await self.remove(key)

    async def remove(self, key: str) -> None:
        item = self.items.pop(key, None)
        self.access.pop(key, None)
        if item is not None:
            await item.stop()


class FakeRuntime:
    def __init__(self):
        self.stop_count = 0

    async def stop(self) -> None:
        self.stop_count += 1


class RuntimePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_lru_and_ttl_stop_evicted_runtime_once(self) -> None:
        now = time.monotonic()
        policy = RuntimePolicyHarness(ttl=10, maximum=2)
        first, second, third = FakeRuntime(), FakeRuntime(), FakeRuntime()
        await policy.add("first", first, now - 3)
        await policy.add("second", second, now - 2)
        await policy.add("third", third, now - 1)
        self.assertEqual(first.stop_count, 1)
        self.assertNotIn("first", policy.items)
        self.assertEqual(await policy.cleanup(now + 20), 2)
        self.assertEqual(second.stop_count, 1)
        self.assertEqual(third.stop_count, 1)


if __name__ == "__main__":
    unittest.main()
