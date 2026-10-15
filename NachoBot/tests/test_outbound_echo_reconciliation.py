"""Offline regressions for the outbound echo/storage race.

This test extracts the production method bodies and supplies only their module
dependencies. It never imports the Core application package or opens its DB.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


CORE_ROOT = Path(__file__).resolve().parents[1]


def _load_pure_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _extract_callable(path: Path, name: str, namespace: dict, *, owner: str | None = None):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if owner is None:
        node = next(
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
        )
    else:
        class_node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == owner)
        node = next(
            item
            for item in class_node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
        )
    node = copy.deepcopy(node)
    node.decorator_list = []
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            node,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class _Condition:
    def __init__(self, predicate):
        self.predicate = predicate

    def __and__(self, other):
        return _Condition(lambda row: self.predicate(row) and other.predicate(row))


class _Field:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, value):
        return _Condition(lambda row: getattr(row, self.name) == value)

    def desc(self):
        return self


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)

    def where(self, condition):
        self.rows = [row for row in self.rows if condition.predicate(row)]
        return self

    def order_by(self, field):
        self.rows.sort(key=lambda row: getattr(row, field.name), reverse=True)
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class _FakeDatabase:
    def __init__(self):
        self.rows = []
        self.update_count = 0

        database = self

        class _Update:
            def __init__(self, changes):
                self.changes = changes
                self.condition = None

            def where(self, condition):
                self.condition = condition
                return self

            def execute(self):
                matched = [row for row in database.rows if self.condition.predicate(row)]
                for row in matched:
                    for name, value in self.changes.items():
                        setattr(row, name, value)
                database.update_count += len(matched)
                return len(matched)

        class Messages:
            id = _Field("id")
            message_id = _Field("message_id")
            chat_info_platform = _Field("chat_info_platform")
            time = _Field("time")

            @classmethod
            def select(cls):
                return _Query(database.rows)

            @classmethod
            def update(cls, **changes):
                return _Update(changes)

            @classmethod
            def create(cls, **values):
                values.setdefault("id", len(database.rows) + 1)
                values.setdefault("time", float(values["id"]))
                row = SimpleNamespace(**values)
                database.rows.append(row)
                return row

        self.Messages = Messages


class _EventManager:
    def __init__(self, after_result=(True, None)):
        self.after_result = after_result

    async def handle_nacho_events(self, event_type, **kwargs):
        if event_type == "after_send":
            return self.after_result
        return True, None


class _Seg:
    def __init__(self, *, type, data):
        self.type = type
        self.data = data


class _Message:
    def __init__(self, message_id="core-message", platform="discord"):
        self.message_info = SimpleNamespace(message_id=message_id, platform=platform)
        self.chat_stream = SimpleNamespace(stream_id="stream-1")
        self.message_segment = SimpleNamespace(type="text", data="original")
        self.processed_plain_text = "original"
        self.display_message = ""
        self.thinking_start_time = 1.0
        self.is_emoji = False
        self.reply = None

    async def process(self):
        return None


class OutboundEchoReconciliationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.echo_module = _load_pure_module(
            "offline_outbound_echo",
            CORE_ROOT / "src" / "chat" / "message_receive" / "outbound_echo.py",
        )

    def setUp(self):
        self.logger = Mock()
        self.registry = self.echo_module.OutboundEchoRegistry(max_records=8, ttl_seconds=300)
        self.database = _FakeDatabase()
        self.store_snapshots = []
        storage_namespace = {"Messages": self.database.Messages, "logger": self.logger}
        update_message = _extract_callable(
            CORE_ROOT / "src" / "chat" / "message_receive" / "storage.py",
            "update_message",
            storage_namespace,
            owner="MessageStorage",
        )
        self.storage_api = type("MessageStorage", (), {"update_message": staticmethod(update_message)})

        self.ack_namespace = {"asyncio": asyncio, "_pending_ack_waiters": {}}
        register = _extract_callable(
            CORE_ROOT / "src" / "plugin_system" / "apis" / "send_api.py",
            "_register_ack_waiter",
            self.ack_namespace,
        )
        self.resolve_ack = _extract_callable(
            CORE_ROOT / "src" / "plugin_system" / "apis" / "send_api.py",
            "resolve_message_ack",
            self.ack_namespace,
        )
        self.remove_ack = _extract_callable(
            CORE_ROOT / "src" / "plugin_system" / "apis" / "send_api.py",
            "_remove_ack_waiter",
            self.ack_namespace,
        )
        self.receipt_message_id = _extract_callable(
            CORE_ROOT / "src" / "plugin_system" / "apis" / "send_api.py",
            "_outgoing_message_id",
            self.ack_namespace,
        )

        self.events = _EventManager()
        self.event_modules = self._event_module_stubs(self.events)
        self.event_modules["src.multimodal"].get_multimodal_router = lambda: object()
        self.echo_method = _extract_callable(
            CORE_ROOT / "src" / "chat" / "message_receive" / "bot.py",
            "echo_message_process",
            {
                "Any": object,
                "Dict": dict,
                "logger": self.logger,
                "send_api": SimpleNamespace(resolve_message_ack=self.resolve_ack),
                "MessageStorage": self.storage_api,
                "outbound_echo_registry": self.registry,
            },
            owner="ChatBot",
        )
        self.bot = type("Bot", (), {"echo_message_process": self.echo_method})()
        self.relay_started = None
        self.relay_release = None
        self.relay_seen_ids = []

        self.sender_namespace = {
            "asyncio": asyncio,
            "logger": self.logger,
            "MessageStorage": self.storage_api,
            "outbound_echo_registry": self.registry,
            "EventType": SimpleNamespace(
                POST_SEND_PRE_PROCESS="pre_send",
                POST_SEND="post_send",
                AFTER_SEND="after_send",
            ),
            "Seg": _Seg,
            "calculate_typing_time": lambda **kwargs: 0,
            "runtime_capabilities_from_stream": lambda stream: SimpleNamespace(
                voice_stream=getattr(self, "voice_stream", False)
            ),
            "_send_message": self._accept,
            "_should_suppress_text_reply": lambda text: False,
            "_tts_text_entries": lambda segment, default_language="": getattr(
                self, "tts_entries", []
            ),
            "_tts_default_language": lambda message, capabilities: "zh",
            "_tts_fields_as_display_text": lambda segment: segment,
            "_router_allows_tts": lambda router: True,
            "_relay_tts_text": self._relay,
        }
        self.sender_method = _extract_callable(
            CORE_ROOT / "src" / "chat" / "message_receive" / "uni_message_sender.py",
            "send_message",
            self.sender_namespace,
            owner="UniversalMessageSender",
        )
        self.sender = SimpleNamespace(storage=SimpleNamespace(store_message=self._store_message))
        self.sender.send_message = self.sender_method.__get__(self.sender, type(self.sender))

    @staticmethod
    def _event_module_stubs(events):
        def package(name):
            module = ModuleType(name)
            module.__path__ = []
            return module

        event_module = ModuleType("src.plugin_system.core.events_manager")
        event_module.events_manager = events
        component_module = ModuleType("src.plugin_system.base.component_types")
        component_module.EventType = SimpleNamespace(
            POST_SEND_PRE_PROCESS="pre_send",
            POST_SEND="post_send",
            AFTER_SEND="after_send",
        )
        return {
            "src": package("src"),
            "src.plugin_system": package("src.plugin_system"),
            "src.plugin_system.core": package("src.plugin_system.core"),
            "src.plugin_system.base": package("src.plugin_system.base"),
            "src.plugin_system.core.events_manager": event_module,
            "src.plugin_system.base.component_types": component_module,
            "src.multimodal": ModuleType("src.multimodal"),
        }

    async def _store_message(self, message, chat_stream):
        self.store_snapshots.append(
            {
                "message_id": message.message_info.message_id,
                "processed_plain_text": message.processed_plain_text,
                "message_segment": message.message_segment,
            }
        )
        return self.database.Messages.create(
            message_id=message.message_info.message_id,
            chat_info_platform=message.message_info.platform,
            chat_id=chat_stream.stream_id,
            time=1.0,
        )

    async def _accept(self, message, **kwargs):
        return True

    async def _relay(self, message, router, text, text_lang):
        self.relay_seen_ids.append(message.message_info.message_id)
        if self.relay_started is not None:
            self.relay_started.set()
        if self.relay_release is not None:
            await self.relay_release.wait()
        return True

    async def _send(self, message, *, storage_message=True):
        with patch.dict(sys.modules, self.event_modules):
            return await self.sender.send_message(message, storage_message=storage_message)

    async def _echo(self, message_id, actual_id, platform="discord"):
        await self.bot.echo_message_process(
            {
                "platform": platform,
                "content": {
                    "type": "echo",
                    "echo": message_id,
                    "actual_id": actual_id,
                },
            }
        )

    def _row(self, message_id):
        return next(row for row in self.database.rows if row.message_id == message_id)

    async def test_early_echo_during_enqueue_waits_through_relay_then_updates_stored_row(self):
        message = _Message("send_api_1")
        self.voice_stream = True
        self.tts_entries = [("spoken", "zh")]
        self.relay_started = asyncio.Event()
        self.relay_release = asyncio.Event()
        ack_future = self.ack_namespace["_register_ack_waiter"]("send_api_1", "discord")

        async def accept_and_echo(outgoing, **kwargs):
            await self._echo("send_api_1", "native-1")
            return True

        self.sender_namespace["_send_message"] = accept_and_echo
        self.sender_namespace["get_multimodal_router"] = lambda: object()
        with patch.dict(sys.modules, self.event_modules):
            task = asyncio.create_task(self.sender.send_message(message))
            await asyncio.wait_for(self.relay_started.wait(), timeout=1)
            self.assertEqual(message.message_info.message_id, "send_api_1")
            self.assertEqual(self.relay_seen_ids, ["send_api_1"])
            self.assertEqual(self.database.rows, [])
            self.relay_release.set()
            self.assertTrue(await asyncio.wait_for(task, timeout=1))

        self.assertEqual(ack_future.result(), "native-1")
        self.assertEqual(message.message_info.message_id, "native-1")
        self.assertEqual(self.database.rows[0].message_id, "native-1")
        self.assertEqual(self.database.update_count, 1)
        self.assertEqual(self.registry.record_count(), 1)
        self.logger.warning.assert_not_called()
        self.assertEqual(
            self.receipt_message_id(message, True, "send_api_1"),
            "native-1",
        )

    async def test_conflicting_echo_cannot_rewrite_row_during_paused_storage(self):
        message = _Message("probe-core")
        ack_future = self.ack_namespace["_register_ack_waiter"]("probe-core", "discord")
        storage_started = asyncio.Event()
        storage_release = asyncio.Event()

        async def pause_after_insert(outgoing, chat_stream):
            row = self.database.Messages.create(
                message_id=outgoing.message_info.message_id,
                chat_info_platform=outgoing.message_info.platform,
                chat_id=chat_stream.stream_id,
                time=1.0,
            )
            storage_started.set()
            await storage_release.wait()
            return row

        async def accept_with_canonical_echo(outgoing, **kwargs):
            await self._echo("probe-core", "first-native")
            return True

        self.sender.storage.store_message = pause_after_insert
        self.sender_namespace["_send_message"] = accept_with_canonical_echo

        with patch.dict(sys.modules, self.event_modules):
            task = asyncio.create_task(self.sender.send_message(message))
            await asyncio.wait_for(storage_started.wait(), timeout=1)
            row = self.database.rows[0]
            self.assertEqual(row.message_id, "probe-core")

            await self._echo("probe-core", "conflicting-native")

            self.assertEqual(row.message_id, "probe-core")
            self.assertEqual(self.database.update_count, 0)
            self.assertEqual(message.message_info.message_id, "probe-core")
            self.assertEqual(ack_future.result(), "first-native")
            self.logger.warning.assert_called_once()
            self.assertIn("冲突", self.logger.warning.call_args.args[0])

            storage_release.set()
            self.assertTrue(await asyncio.wait_for(task, timeout=1))

        self.assertEqual(row.message_id, "first-native")
        self.assertEqual(message.message_info.message_id, "first-native")
        self.assertEqual(self.database.update_count, 1)

    async def test_late_echo_numeric_id_duplicate_and_unknown_ack_are_scoped(self):
        message = _Message("send_api_2")
        self.assertTrue(await self._send(message))
        ack_future = self.ack_namespace["_register_ack_waiter"]("send_api_2", "discord")

        await self._echo("send_api_2", 123456789012345678)
        self.assertEqual(ack_future.result(), "123456789012345678")
        self.assertEqual(message.message_info.message_id, "123456789012345678")
        self.assertEqual(self.database.rows[0].message_id, "123456789012345678")
        self.assertEqual(self.database.update_count, 1)

        await self._echo("send_api_2", "123456789012345678")
        self.assertEqual(self.database.update_count, 1)

        self.database.Messages.create(
            message_id="unrelated-native-id",
            chat_info_platform="discord",
            chat_id="other-stream",
            time=2.0,
        )
        await self._echo("unknown-core-id", "unrelated-native-id")
        self.assertEqual(
            [row.message_id for row in self.database.rows],
            ["123456789012345678", "unrelated-native-id"],
        )
        self.logger.warning.assert_called_once()

    async def test_wrong_platform_ack_does_not_resolve_or_mutate_another_route(self):
        message = _Message("same-core-id", platform="napcat")
        self.assertTrue(await self._send(message))
        ack_future = self.ack_namespace["_register_ack_waiter"]("same-core-id", "napcat")

        await self._echo("same-core-id", "wrong-native-id", platform="discord")

        self.assertEqual(self.database.rows[0].message_id, "same-core-id")
        self.assertEqual(message.message_info.message_id, "same-core-id")
        self.assertFalse(ack_future.done())
        self.logger.warning.assert_called_once()
        self.ack_namespace["_remove_ack_waiter"]("same-core-id", "napcat")

    async def test_storage_api_remains_compatible_without_platform_argument(self):
        message = _Message("legacy-core-id", platform="napcat")
        self.assertTrue(await self._send(message))

        self.assertTrue(self.storage_api.update_message("legacy-core-id", "legacy-native-id"))
        self.assertEqual(self.database.rows[0].message_id, "legacy-native-id")

    async def test_after_send_modification_is_stored_and_post_send_cancel_leaks_nothing(self):
        modified = SimpleNamespace(
            _modify_flags=SimpleNamespace(modify_message_segments=True, modify_plain_text=True),
            message_segments=[_Seg(type="text", data="modified")],
            plain_text="modified",
        )
        self.events.after_result = (True, modified)
        message = _Message("modified-core")

        async def accept_and_echo(outgoing, **kwargs):
            await self._echo("modified-core", "modified-native")
            return True

        self.sender_namespace["_send_message"] = accept_and_echo
        self.assertTrue(await self._send(message))
        self.assertEqual(message.message_info.message_id, "modified-native")
        self.assertEqual(self.database.rows[0].message_id, "modified-native")
        self.assertEqual(self.store_snapshots[0]["processed_plain_text"], "modified")
        self.assertEqual(self.store_snapshots[0]["message_segment"].data[0].data, "modified")

        self.events.after_result = (False, None)
        message2 = _Message("cancelled-core")
        async def accept_and_echo_cancelled(outgoing, **kwargs):
            await self._echo("cancelled-core", "cancelled-native")
            return True
        self.sender_namespace["_send_message"] = accept_and_echo_cancelled
        self.assertTrue(await self._send(message2))
        self.assertEqual(message2.message_info.message_id, "cancelled-core")
        self.assertEqual(self.registry.record_count(), 1)
        self.assertEqual([row.message_id for row in self.database.rows], ["modified-native"])

    async def test_enqueue_failure_exception_cancellation_and_unstored_send_cleanup(self):
        failed = _Message("failed-core")
        self.sender_namespace["_send_message"] = lambda *args, **kwargs: _async_value(False)
        self.assertFalse(await self._send(failed))
        self.assertEqual(self.registry.record_count(), 0)

        errored = _Message("error-core")
        self.sender_namespace["_send_message"] = _raise_error
        with self.assertRaisesRegex(RuntimeError, "enqueue failed"):
            await self._send(errored)
        self.assertEqual(self.registry.record_count(), 0)

        cancelled = _Message("cancel-core")
        self.sender_namespace["_send_message"] = _raise_cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self._send(cancelled)
        self.assertEqual(self.registry.record_count(), 0)

        unstored = _Message("unstored-core")
        async def accept_and_echo(outgoing, **kwargs):
            await self._echo("unstored-core", "unstored-native")
            return True
        self.sender_namespace["_send_message"] = accept_and_echo
        self.assertTrue(await self._send(unstored, storage_message=False))
        self.assertEqual(unstored.message_info.message_id, "unstored-native")
        self.assertEqual(self.database.rows, [])
        self.assertEqual(self.registry.record_count(), 0)

    def test_registry_is_bounded_and_expires_completed_records(self):
        now = [0.0]
        registry = self.echo_module.OutboundEchoRegistry(
            max_records=2,
            ttl_seconds=10,
            clock=lambda: now[0],
        )
        messages = [_Message(f"bounded-{index}") for index in range(3)]
        handles = [registry.register(message) for message in messages]
        self.assertEqual(registry.record_count(), 2)
        self.assertFalse(registry.observe_echo("bounded-0", "discord", "native-0").matched)
        self.assertIsNone(registry.mark_stored(handles[0]))
        self.assertIsNone(registry.mark_stored(handles[1]))
        self.assertIsNone(registry.mark_stored(handles[2]))
        now[0] = 11.0
        self.assertEqual(registry.record_count(), 0)


async def _async_value(value):
    return value


async def _raise_error(*args, **kwargs):
    raise RuntimeError("enqueue failed")


async def _raise_cancelled(*args, **kwargs):
    raise asyncio.CancelledError


if __name__ == "__main__":
    unittest.main()
