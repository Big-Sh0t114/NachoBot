"""摘要双写任务 drain 的隔离单元测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class Prompt:
    def __init__(self, *_args, **_kwargs):
        pass


@contextmanager
def isolated_summarizer_module():
    module_name = "_nachobot_summarizer_test"
    names = {
        module_name,
        "json_repair",
        "src",
        "src.common",
        "src.common.logger",
        "src.common.data_models",
        "src.common.data_models.database_data_model",
        "src.config",
        "src.config.config",
        "src.llm_models",
        "src.llm_models.utils_model",
        "src.plugin_system",
        "src.plugin_system.apis",
        "src.common.database",
        "src.common.database.database_model",
        "src.memory_system",
        "src.memory_system.memory_service",
        "src.chat",
        "src.chat.utils",
        "src.chat.utils.chat_message_builder",
        "src.chat.utils.prompt_builder",
        "src.chat.message_receive",
        "src.chat.message_receive.chat_stream",
        "src.person_info",
        "src.person_info.person_info",
    }
    previous = {name: sys.modules.get(name) for name in names}

    def install(name: str, **attributes):
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        sys.modules[name] = module
        return module

    for package_name in (
        "src",
        "src.common",
        "src.common.data_models",
        "src.config",
        "src.llm_models",
        "src.plugin_system",
        "src.chat",
        "src.chat.utils",
        "src.chat.message_receive",
        "src.person_info",
    ):
        package = install(package_name)
        package.__path__ = []

    install("json_repair", repair_json=lambda value, **_kwargs: value)
    install("src.common.logger", get_logger=lambda _name: _Logger())
    install("src.common.data_models.database_data_model", DatabaseMessages=object)
    install("src.common.database")
    install("src.common.database.database_model", ChatHistory=object)
    model_config = types.SimpleNamespace(model_task_config=types.SimpleNamespace(utils=object()))
    install("src.config.config", model_config=model_config)
    install("src.llm_models.utils_model", LLMRequest=lambda **_kwargs: object())
    install(
        "src.plugin_system.apis",
        message_api=types.SimpleNamespace(),
        database_api=types.SimpleNamespace(db_save=AsyncMock(return_value=object())),
    )
    install(
        "src.memory_system",
    )
    memory_service = types.SimpleNamespace(is_enabled=lambda: False)
    install("src.memory_system.memory_service", memory_service=memory_service)
    install(
        "src.chat.utils.chat_message_builder",
        build_readable_messages=lambda *_args, **_kwargs: "",
        get_raw_msg_after_cursor_with_chat=lambda *_args, **_kwargs: [],
    )
    install("src.person_info.person_info", Person=object)
    manager = types.SimpleNamespace(get_stream_name=lambda _chat_id: None)
    install("src.chat.message_receive.chat_stream", get_chat_manager=lambda: manager)
    install(
        "src.chat.utils.prompt_builder",
        Prompt=Prompt,
        global_prompt_manager=types.SimpleNamespace(),
    )

    spec = importlib.util.spec_from_file_location(
        module_name,
        PROJECT_ROOT / "src/memory_system/chat_history_summarizer.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load chat_history_summarizer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class SummarizerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _message(module, message_id: str, timestamp: float, *, bot: bool = True):
        user_id = "bot" if bot else "human"
        platform = "test"
        info = types.SimpleNamespace(platform=platform, user_id=user_id)
        return types.SimpleNamespace(
            message_id=message_id,
            time=timestamp,
            user_info=info,
            processed_plain_text=f"text-{message_id}",
            display_message=f"text-{message_id}",
            user_id=user_id,
            user_platform=platform,
            chat_info=types.SimpleNamespace(platform=platform),
        )

    def _make_topic_summarizer(self, module, *, db_save=True):
        summarizer = module.ChatHistorySummarizer("chat")
        summarizer._build_numbered_messages_for_llm = lambda messages: (
            [f"{i}. text" for i, _ in enumerate(messages, 1)],
            {i: f"{i}. text" for i, _ in enumerate(messages, 1)},
            {i: f"text-{message.message_id}" for i, message in enumerate(messages, 1)},
            {i: {"tester"} for i, _ in enumerate(messages, 1)},
        )
        module.is_bot_self = lambda _platform, user_id: user_id == "bot"
        module.global_prompt_manager.format_prompt = AsyncMock(return_value="prompt")
        sys.modules["src.plugin_system.apis"].database_api = types.SimpleNamespace(
            db_save=AsyncMock(return_value=db_save)
        )
        return summarizer

    async def test_stop_waits_for_writeback_completion(self) -> None:
        with isolated_summarizer_module() as module:
            summarizer = module.ChatHistorySummarizer("chat", writeback_drain_timeout=0.5)
            finished = asyncio.Event()

            async def writeback():
                await asyncio.sleep(0.01)
                finished.set()

            task = asyncio.create_task(writeback())
            summarizer._writeback_tasks.add(task)
            task.add_done_callback(summarizer._writeback_tasks.discard)
            await summarizer.stop()
            self.assertTrue(finished.is_set())
            self.assertTrue(task.done())
            self.assertFalse(summarizer._writeback_tasks)

    async def test_stop_cancels_writeback_after_timeout(self) -> None:
        with isolated_summarizer_module() as module:
            summarizer = module.ChatHistorySummarizer("chat", writeback_drain_timeout=0)

            async def writeback():
                await asyncio.Event().wait()

            task = asyncio.create_task(writeback())
            summarizer._writeback_tasks.add(task)
            task.add_done_callback(summarizer._writeback_tasks.discard)
            await asyncio.sleep(0)
            await summarizer.stop()
            self.assertTrue(task.cancelled())
            self.assertFalse(summarizer._writeback_tasks)

    async def test_stop_has_strict_timeout_and_keeps_resistant_writeback_tracked(self) -> None:
        with isolated_summarizer_module() as module:
            summarizer = module.ChatHistorySummarizer("chat", writeback_drain_timeout=0.01)
            started = asyncio.Event()
            release = asyncio.Event()

            async def writeback():
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()

            task = asyncio.create_task(writeback())
            summarizer._writeback_tasks.add(task)
            task.add_done_callback(summarizer._writeback_tasks.discard)
            await started.wait()

            started_at = asyncio.get_running_loop().time()
            await summarizer.stop()
            elapsed = asyncio.get_running_loop().time() - started_at
            self.assertLess(elapsed, 0.2)
            self.assertFalse(task.done())
            self.assertIn(task, summarizer._writeback_tasks)

            release.set()
            await asyncio.wait_for(task, timeout=0.5)

    async def test_stop_bounds_resistant_periodic_producer_and_blocks_duplicate_start(self) -> None:
        with isolated_summarizer_module() as module:
            summarizer = module.ChatHistorySummarizer("chat", writeback_drain_timeout=0.01)
            started = asyncio.Event()
            release = asyncio.Event()

            async def resistant_producer():
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()

            summarizer._periodic_check_loop = resistant_producer
            summarizer._load_topic_cache_from_disk = lambda: None
            summarizer._load_batch_from_disk = lambda: asyncio.sleep(0)
            await summarizer.start()
            producer = summarizer._periodic_task
            self.assertIsNotNone(producer)
            await started.wait()

            started_at = asyncio.get_running_loop().time()
            await summarizer.stop()
            elapsed = asyncio.get_running_loop().time() - started_at
            self.assertLess(elapsed, 0.2)
            self.assertIs(producer, summarizer._periodic_task)
            self.assertFalse(producer.done())

            await summarizer.start()
            self.assertIs(producer, summarizer._periodic_task)

            release.set()
            await asyncio.wait_for(asyncio.shield(producer), timeout=0.5)
            await asyncio.sleep(0)
            self.assertIsNone(summarizer._periodic_task)

            await summarizer.start()
            replacement = summarizer._periodic_task
            self.assertIsNotNone(replacement)
            self.assertIsNot(producer, replacement)
            release.set()
            await asyncio.wait_for(asyncio.shield(replacement), timeout=0.5)

    async def test_concurrent_start_serializes_disk_load_and_publishes_one_producer(self) -> None:
        with isolated_summarizer_module() as module:
            summarizer = module.ChatHistorySummarizer("chat", writeback_drain_timeout=0.1)
            entered = asyncio.Event()
            release = asyncio.Event()
            load_calls = 0

            async def load_batch():
                nonlocal load_calls
                load_calls += 1
                entered.set()
                await release.wait()

            async def producer():
                await asyncio.Event().wait()

            summarizer._load_topic_cache_from_disk = lambda: None
            summarizer._load_batch_from_disk = load_batch
            summarizer._periodic_check_loop = producer
            first = asyncio.create_task(summarizer.start())
            await entered.wait()
            second = asyncio.create_task(summarizer.start())
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            release.set()
            await asyncio.gather(first, second)
            self.assertEqual(load_calls, 1)
            periodic = summarizer._periodic_task
            self.assertIsNotNone(periodic)
            await summarizer.stop()

    async def test_stop_waits_for_in_progress_start_before_returning(self) -> None:
        with isolated_summarizer_module() as module:
            summarizer = module.ChatHistorySummarizer("chat", writeback_drain_timeout=0.1)
            entered = asyncio.Event()
            release = asyncio.Event()

            async def load_batch():
                entered.set()
                await release.wait()

            summarizer._load_topic_cache_from_disk = lambda: None
            summarizer._load_batch_from_disk = load_batch
            summarizer._periodic_check_loop = lambda: asyncio.sleep(60)
            starting = asyncio.create_task(summarizer.start())
            await entered.wait()
            stopping = asyncio.create_task(summarizer.stop())
            await asyncio.sleep(0)
            self.assertFalse(stopping.done())
            release.set()
            await asyncio.gather(starting, stopping)
            self.assertFalse(summarizer._running)
            self.assertIsNone(summarizer._periodic_task)

    async def test_same_timestamp_cursor_advances_by_message_id(self) -> None:
        with isolated_summarizer_module() as module:
            import tempfile

            class Message:
                def __init__(self, message_id: str, timestamp: float) -> None:
                    self.message_id = message_id
                    self.time = timestamp

            messages = [Message("b", 11.0), Message("c", 11.0)]
            calls = []

            def query(chat_id, cursor_time, cursor_message_id, **_kwargs):
                calls.append((chat_id, cursor_time, cursor_message_id))
                return list(messages)

            import src.chat.utils.chat_message_builder as builder

            builder.get_raw_msg_after_cursor_with_chat = query
            with tempfile.TemporaryDirectory() as temp_dir:
                summarizer = module.ChatHistorySummarizer("chat")
                summarizer._topic_cache_file = Path(temp_dir) / "cursor.json"
                summarizer.last_check_time = 10.0
                summarizer.last_check_message_id = "a"
                summarizer._check_and_run_topic_check = lambda *_args: None

                async def no_topic_check(*_args):
                    return None

                summarizer._check_and_run_topic_check = no_topic_check
                await summarizer.process(current_time=20.0)

                self.assertEqual(calls, [("chat", 10.0, "a")])
                self.assertEqual(
                    (summarizer.last_check_time, summarizer.last_check_message_id),
                    (11.0, "c"),
                )
                self.assertEqual(
                    [message.message_id for message in summarizer.current_batch.messages],
                    ["b", "c"],
                )

    async def test_three_failed_analysis_attempts_retain_batch_cursor_and_disk_checkpoint(self) -> None:
        with isolated_summarizer_module() as module:
            import tempfile

            summarizer = self._make_topic_summarizer(module)
            messages = [self._message(module, f"m{i}", float(i)) for i in range(80)]
            summarizer.current_batch = module.MessageBatch(messages, 0.0, 79.0)
            summarizer.last_check_time = 1.0
            summarizer.last_check_message_id = "m1"
            summarizer._topic_cache_file = Path(tempfile.mkdtemp()) / "cache.json"
            summarizer._analyze_topics_with_llm = AsyncMock(return_value=(False, {}))

            consumed = await summarizer._check_and_run_topic_check(100.0)

            self.assertFalse(consumed)
            self.assertIsNotNone(summarizer.current_batch)
            self.assertEqual((summarizer.last_check_time, summarizer.last_check_message_id), (1.0, "m1"))
            persisted = __import__("json").loads(summarizer._topic_cache_file.read_text(encoding="utf-8"))
            self.assertIn("current_batch", persisted)
            self.assertEqual(summarizer._analyze_topics_with_llm.await_count, 3)

    async def test_out_of_range_topic_indices_retain_batch(self) -> None:
        with isolated_summarizer_module() as module:
            summarizer = self._make_topic_summarizer(module)
            messages = [self._message(module, "m1", 1.0)] * 80
            summarizer.current_batch = module.MessageBatch(messages, 1.0, 1.0)
            summarizer._analyze_topics_with_llm = AsyncMock(
                side_effect=[(True, {"topic": [999]})] * 3
            )
            consumed = await summarizer._check_and_run_topic_check(100.0)
            self.assertFalse(consumed)
            self.assertIsNotNone(summarizer.current_batch)

    async def test_compression_failure_retains_and_persists_finalizable_topic(self) -> None:
        with isolated_summarizer_module() as module:
            import tempfile

            summarizer = self._make_topic_summarizer(module)
            summarizer._topic_cache_file = Path(tempfile.mkdtemp()) / "cache.json"
            item = module.TopicCacheItem(
                topic="topic",
                messages=["one"] * 6,
                source_message_keys=[(1.0, "m1"), (2.0, "m2")],
            )
            summarizer.topic_cache["topic"] = item
            summarizer._compress_with_llm = AsyncMock(return_value=(False, [], "", []))
            summarizer._analyze_topics_with_llm = AsyncMock(return_value=(True, {"topic": [1]}))
            await summarizer._run_topic_check_and_update_cache([self._message(module, "m3", 3.0)])
            self.assertIn("topic", summarizer.topic_cache)
            persisted = __import__("json").loads(summarizer._topic_cache_file.read_text(encoding="utf-8"))
            self.assertIn("topic", persisted["topics"])

    async def test_db_save_false_retains_topic_and_does_not_schedule_memorix(self) -> None:
        with isolated_summarizer_module() as module:
            summarizer = self._make_topic_summarizer(module, db_save=False)
            import tempfile

            summarizer._topic_cache_file = Path(tempfile.mkdtemp()) / "cache.json"
            item = module.TopicCacheItem(
                topic="topic",
                messages=["one"] * 6,
                source_message_keys=[(1.0, "m1")],
            )
            summarizer.topic_cache["topic"] = item
            summarizer._compress_with_llm = AsyncMock(return_value=(True, ["k"], "summary", ["point"]))
            summarizer._analyze_topics_with_llm = AsyncMock(return_value=(True, {"topic": [1]}))
            await summarizer._run_topic_check_and_update_cache([self._message(module, "m2", 2.0)])
            self.assertIn("topic", summarizer.topic_cache)
            persisted = __import__("json").loads(summarizer._topic_cache_file.read_text(encoding="utf-8"))
            self.assertIn("topic", persisted["topics"])
            self.assertFalse(summarizer._writeback_tasks)

    async def test_primary_save_removes_topic_and_schedules_secondary_writeback(self) -> None:
        with isolated_summarizer_module() as module:
            summarizer = self._make_topic_summarizer(module, db_save=True)
            sys.modules["src.memory_system.memory_service"].memory_service.is_enabled = lambda: True
            item = module.TopicCacheItem(
                topic="topic",
                messages=["one"] * 6,
                source_message_keys=[(1.0, "m1")],
            )
            summarizer.topic_cache["topic"] = item
            summarizer._compress_with_llm = AsyncMock(return_value=(True, ["k"], "summary", ["point"]))
            summarizer._writeback_to_a_memorix = AsyncMock()
            summarizer._analyze_topics_with_llm = AsyncMock(return_value=(True, {"topic": [1]}))
            await summarizer._run_topic_check_and_update_cache([self._message(module, "m2", 2.0)])
            await asyncio.sleep(0)
            self.assertFalse(summarizer.topic_cache)
            self.assertTrue(summarizer._writeback_to_a_memorix.await_count == 1)

    async def test_multi_batch_source_identity_and_range_ignore_generated_summary(self) -> None:
        with isolated_summarizer_module() as module:
            summarizer = self._make_topic_summarizer(module)
            first = self._message(module, "m1", 1.0)
            second = self._message(module, "m2", 5.0)
            summarizer._analyze_topics_with_llm = AsyncMock(
                side_effect=[(True, {"topic-a": [1]}), (True, {"topic-a": [1]})]
            )
            await summarizer._run_topic_check_and_update_cache([first])
            await summarizer._run_topic_check_and_update_cache([second])
            item = summarizer.topic_cache["topic-a"]
            self.assertEqual(item.source_start_time, 1.0)
            self.assertEqual(item.source_end_time, 5.0)
            identity = item.source_identity
            item.topic = "topic-b"
            summarizer._refresh_topic_source_identity("topic-b", item)
            self.assertEqual(identity, item.source_identity)

            import tempfile

            with tempfile.TemporaryDirectory() as temp_dir:
                summarizer._topic_cache_file = Path(temp_dir) / "cache.json"
                summarizer._persist_topic_cache()
                restored = module.ChatHistorySummarizer("chat")
                restored._topic_cache_file = summarizer._topic_cache_file
                restored._load_topic_cache_from_disk()
                restored_item = restored.topic_cache["topic-a"]
                restored._refresh_topic_source_identity("topic-a", restored_item)
                self.assertEqual(identity, restored_item.source_identity)

            ingest = AsyncMock(return_value={"stored_ids": ["id"]})
            memory_service = sys.modules["src.memory_system.memory_service"].memory_service
            memory_service.ingest_summary = ingest
            await summarizer._writeback_to_a_memorix(
                "chat", 1.0, 5.0, "topic-a", "summary-one", ["k"], [], source_identity=identity
            )
            await summarizer._writeback_to_a_memorix(
                "chat", 1.0, 5.0, "topic-b", "summary-two", ["k2"], [], source_identity=identity
            )
            ids = [call.kwargs["external_id"] for call in ingest.await_args_list]
            self.assertEqual(ids[0], ids[1])


if __name__ == "__main__":
    unittest.main()
