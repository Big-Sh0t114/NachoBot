import asyncio
import copy
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from ncnk_message import UserInfo
from src.chat.message_receive import bot as bot_module
from src.chat.message_receive.delivery_context import scoped_delivery_target
from src.chat.message_receive.message import MessageRecv
from src.plugin_system.apis import send_api
from src.plugin_system.base.base_command import BaseCommand


def _source_message(stream, target, message_id="source-message"):
    message = MessageRecv(
        {
            "message_info": {
                "platform": "discord",
                "message_id": message_id,
                "user_info": {"platform": "discord", "user_id": "user-1", "user_nickname": "User"},
                "additional_config": {"delivery_target": target},
            },
            "message_segment": {"type": "text", "data": "#slow"},
            "raw_message": "#slow",
            "processed_plain_text": "#slow",
        }
    )
    message.chat_stream = stream
    return message


def _stream(stream_id, target, *, platform="discord"):
    latest = _source_message(SimpleNamespace(stream_id=stream_id), target, f"latest-{stream_id}")
    return SimpleNamespace(
        stream_id=stream_id,
        platform=platform,
        user_info=UserInfo(platform=platform, user_id="user-1", user_nickname="User"),
        group_info=None,
        context=SimpleNamespace(message=latest),
    )


class _ChatManager:
    def __init__(self, streams):
        self.streams = streams

    def get_stream(self, stream_id):
        return self.streams.get(str(stream_id))


class _CaptureSender:
    def __init__(self, captured):
        self.captured = captured

    async def send_message(self, message, **_kwargs):
        self.captured.append(
            (
                str(message.message_segment.data),
                copy.deepcopy(message.message_info.additional_config),
            )
        )
        return SimpleNamespace(message_info=SimpleNamespace(message_id=f"sent-{len(self.captured)}"))


class DeliveryTargetScopeTests(unittest.IsolatedAsyncioTestCase):
    def _patch_send_api(self, stack, streams, captured):
        stack.enter_context(patch.object(send_api, "get_chat_manager", return_value=_ChatManager(streams)))
        stack.enter_context(patch.object(send_api, "UniversalMessageSender", side_effect=lambda: _CaptureSender(captured)))
        stack.enter_context(patch.object(send_api, "_should_suppress_reply_by_policy", return_value=False))

    def _patch_command_registry(self, stack, command_class):
        command_info = SimpleNamespace(plugin_name="test", name="slow")
        stack.enter_context(
            patch.object(
                bot_module.component_registry,
                "find_command_by_text",
                return_value=(command_class, {}, command_info),
            )
        )
        stack.enter_context(patch.object(bot_module.component_registry, "get_plugin_config", return_value={}))
        stack.enter_context(patch.object(bot_module.global_announcement_manager, "get_disabled_chat_commands", return_value=set()))

    async def test_early_advanced_commands_keep_source_target_on_every_return_path(self):
        cases = (
            ("allowed-direct", True, False, "高级模式已开启，请尽情使唤NachoBot哦~"),
            ("group-denied", True, True, "group not allowed"),
            ("not-allowed", False, False, "not allowed"),
        )
        for name, allowed, is_group, expected_response in cases:
            with self.subTest(path=name):
                stream_id = f"slash-{name}"
                source_target = {
                    "channel": f"source-{name}",
                    "voice_generation": f"generation-{name}",
                    "correlation_key": f"opaque-{name}",
                }
                latest_target = {
                    "channel": f"latest-{name}",
                    "voice_generation": f"latest-generation-{name}",
                    "correlation_key": f"latest-opaque-{name}",
                }
                latest_stream = _stream(stream_id, latest_target)
                source_stream = SimpleNamespace(
                    stream_id=stream_id,
                    platform="discord",
                    group_info=SimpleNamespace(group_id=f"group-{name}") if is_group else None,
                )
                source = _source_message(source_stream, source_target, f"source-{name}")
                source.processed_plain_text = "#adv_on"
                source.raw_message = "#adv_on"
                captured = []

                stack = ExitStack()
                self._patch_send_api(stack, {stream_id: latest_stream}, captured)
                stack.enter_context(patch.object(bot_module.advanced_manager, "is_admin", return_value=False))
                stack.enter_context(patch.object(bot_module.advanced_manager, "is_allowed", return_value=allowed))
                stack.enter_context(patch.object(bot_module.advanced_manager, "set_state"))
                stack.enter_context(patch.object(bot_module.advanced_manager, "set_group_state"))
                with stack:
                    result = await bot_module.ChatBot()._process_commands_with_new_system(source)

                self.assertTrue(result[0])
                self.assertEqual(result[1], expected_response)
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0][1]["delivery_target"], source_target)

    async def test_slow_command_sends_with_original_target_after_stream_context_changes(self):
        stream_id = "discord:group:user"
        source_target = {"channel": "channel-A", "voice_generation": "generation-A"}
        stream = _stream(stream_id, {"channel": "channel-A", "voice_generation": "generation-A"})
        source_stream = SimpleNamespace(stream_id=stream_id)
        source = _source_message(source_stream, source_target)
        started = asyncio.Event()
        release = asyncio.Event()
        captured = []

        class SlowCommand(BaseCommand):
            async def execute(self):
                started.set()
                await release.wait()
                await self.send_text("slow result", storage_message=False)
                return True, "sent", True

        stack = ExitStack()
        self._patch_send_api(stack, {stream_id: stream}, captured)
        self._patch_command_registry(stack, SlowCommand)
        with stack:
            task = asyncio.create_task(bot_module.ChatBot()._process_commands_with_new_system(source))
            await started.wait()
            source_target["voice_generation"] = "mutated-original"
            stream.context = SimpleNamespace(
                message=_source_message(source_stream, {"channel": "channel-B", "voice_generation": "generation-B"})
            )
            release.set()
            result = await task

        self.assertEqual(result, (True, "sent", False))
        self.assertEqual(captured[0][0], "slow result")
        self.assertEqual(
            captured[0][1]["delivery_target"],
            {"channel": "channel-A", "voice_generation": "generation-A"},
        )

    async def test_concurrent_command_scopes_keep_their_own_target(self):
        stream_id = "shared-stream"
        stream = _stream(stream_id, {"channel": "latest", "voice_generation": "latest"})
        captured = []
        ready = 0
        both_ready = asyncio.Event()
        release = asyncio.Event()

        async def send_for(target_name):
            nonlocal ready
            source = _source_message(
                SimpleNamespace(stream_id=stream_id),
                {"channel": target_name, "voice_generation": target_name},
                f"source-{target_name}",
            )
            with scoped_delivery_target(source):
                ready += 1
                if ready == 2:
                    both_ready.set()
                await release.wait()
                await send_api.text_to_stream(target_name, stream_id, storage_message=False)

        stack = ExitStack()
        self._patch_send_api(stack, {stream_id: stream}, captured)
        with stack:
            tasks = [asyncio.create_task(send_for("target-A")), asyncio.create_task(send_for("target-B"))]
            await both_ready.wait()
            stream.context = SimpleNamespace(
                message=_source_message(SimpleNamespace(stream_id=stream_id), {"channel": "latest-C"})
            )
            release.set()
            await asyncio.gather(*tasks)

        captured_targets = {text: config.get("delivery_target") for text, config in captured}
        self.assertEqual(
            captured_targets,
            {
                "target-A": {"channel": "target-A", "voice_generation": "target-A"},
                "target-B": {"channel": "target-B", "voice_generation": "target-B"},
            },
        )

    async def test_active_scope_does_not_leak_target_to_another_stream(self):
        source_stream_id = "source-stream"
        other_stream_id = "other-stream"
        source = _source_message(
            SimpleNamespace(stream_id=source_stream_id),
            {"channel": "source-channel", "voice_generation": "source-generation"},
        )
        other_stream = _stream(
            other_stream_id,
            {"channel": "other-channel", "voice_generation": "other-generation"},
        )
        captured = []

        stack = ExitStack()
        self._patch_send_api(stack, {other_stream_id: other_stream}, captured)
        with stack, scoped_delivery_target(source):
            delivered = await send_api.text_to_stream("cross-stream", other_stream_id, storage_message=False)

        self.assertTrue(delivered)
        self.assertEqual(captured[0][0], "cross-stream")
        self.assertNotIn("delivery_target", captured[0][1])

    async def test_exception_and_cancellation_restore_unscoped_legacy_behavior(self):
        stream_id = "discord:group:user"
        source = _source_message(
            SimpleNamespace(stream_id=stream_id),
            {"channel": "target-A", "voice_generation": "generation-A"},
        )
        stream = _stream(stream_id, {"channel": "target-A", "voice_generation": "generation-A"})
        started = asyncio.Event()
        release = asyncio.Event()
        captured = []

        class FailingCommand(BaseCommand):
            async def execute(self):
                started.set()
                await release.wait()
                raise RuntimeError("command failed")

        stack = ExitStack()
        self._patch_send_api(stack, {stream_id: stream}, captured)
        self._patch_command_registry(stack, FailingCommand)
        with stack:
            task = asyncio.create_task(bot_module.ChatBot()._process_commands_with_new_system(source))
            await started.wait()
            stream.context = SimpleNamespace(
                message=_source_message(SimpleNamespace(stream_id=stream_id), {"channel": "target-B"})
            )
            release.set()
            result = await task
            await send_api.text_to_stream("after error", stream_id, storage_message=False)

        self.assertEqual(result, (True, "command failed", False))
        self.assertEqual(captured[0][0], "命令执行出错: command failed")
        self.assertEqual(
            captured[0][1]["delivery_target"],
            {"channel": "target-A", "voice_generation": "generation-A"},
        )
        self.assertEqual(captured[1][0], "after error")
        self.assertEqual(captured[1][1]["delivery_target"], {"channel": "target-B"})

        started.clear()

        class CancelledCommand(BaseCommand):
            async def execute(self):
                started.set()
                await asyncio.Event().wait()

        # Reapply send API and command patches for the cancelled dispatch.
        cancel_stack = ExitStack()
        self._patch_send_api(cancel_stack, {stream_id: stream}, captured)
        cancel_stack.enter_context(
            patch.object(
                bot_module.component_registry,
                "find_command_by_text",
                return_value=(CancelledCommand, {}, SimpleNamespace(plugin_name="test", name="slow")),
            )
        )
        cancel_stack.enter_context(patch.object(bot_module.component_registry, "get_plugin_config", return_value={}))
        cancel_stack.enter_context(
            patch.object(bot_module.global_announcement_manager, "get_disabled_chat_commands", return_value=set())
        )
        with cancel_stack:
            task = asyncio.create_task(bot_module.ChatBot()._process_commands_with_new_system(source))
            await started.wait()
            stream.context = SimpleNamespace(
                message=_source_message(SimpleNamespace(stream_id=stream_id), {"channel": "target-C"})
            )
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await send_api.text_to_stream("after cancel", stream_id, storage_message=False)

        self.assertEqual(captured[-1][0], "after cancel")
        self.assertEqual(captured[-1][1]["delivery_target"], {"channel": "target-C"})

    async def test_unscoped_non_discord_send_keeps_latest_context_target(self):
        stream_id = "universal-vc-stream"
        stream = _stream(
            stream_id,
            {"channel": "current-channel", "voice_generation": "current-generation"},
            platform="UniversalVC",
        )
        captured = []

        stack = ExitStack()
        self._patch_send_api(stack, {stream_id: stream}, captured)
        with stack:
            delivered = await send_api.text_to_stream("legacy send", stream_id, storage_message=False)

        self.assertTrue(delivered)
        self.assertEqual(captured[0][0], "legacy send")
        self.assertEqual(
            captured[0][1]["delivery_target"],
            {"channel": "current-channel", "voice_generation": "current-generation"},
        )


if __name__ == "__main__":
    unittest.main()
