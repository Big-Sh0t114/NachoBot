import asyncio
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "NachoBot-Multimodal-Adapter") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "NachoBot-Multimodal-Adapter"))

import adapter as adapter_module
from adapter import BilibiliAdapter
from bili_src.audio.tts_manager import TTSManager
import bili_src.live.event_manager as event_manager_module
from bili_src.live.event_manager import EventManager
from bili_src.live.live_worker import LiveRoomWorker


class _Model:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def tts(self, **kwargs):
        self.calls.append(kwargs)
        return self.name.encode("ascii")


class _Runtime:
    def __init__(self, model):
        self.model = model
        self.entries = 0

    def model_context(self):
        runtime = self

        class Context:
            async def __aenter__(self):
                runtime.entries += 1
                return runtime.model

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return Context()


class BiliTTSHotReloadTests(unittest.TestCase):
    def test_idle_and_buffered_segments_share_refresh_boundary(self):
        async def scenario():
            first = _Model("vox")
            second = _Model("gpt")
            runtime = _Runtime(first)
            manager = TTSManager.__new__(TTSManager)
            manager._tts_runtime = runtime
            manager.tts_model = first

            idle_audio = await manager._synthesize_tts_segment(
                "idle", platform="bilibili", preset_name=None, split_method="cut0"
            )
            runtime.model = second
            buffered_audio = await manager._synthesize_tts_segment(
                "buffered", platform="bilibili", preset_name=None, split_method="cut0"
            )

            self.assertEqual(idle_audio, b"vox")
            self.assertEqual(buffered_audio, b"gpt")
            self.assertEqual(runtime.entries, 2)
            self.assertIs(manager.tts_model, second)
            self.assertEqual(first.calls[0]["platform"], "bilibili")
            self.assertEqual(second.calls[0]["platform"], "bilibili")

        asyncio.run(scenario())


class LiveEventFilterTests(unittest.TestCase):
    def test_transport_and_stat_events_do_not_enter_semantic_handlers(self):
        async def scenario():
            worker = LiveRoomWorker.__new__(LiveRoomWorker)
            worker.logger = SimpleNamespace(debug=Mock())
            worker._handle_danmu_event = AsyncMock()
            worker._handle_gift_event = AsyncMock()
            worker._handle_superchat_event = AsyncMock()
            worker._handle_guard_event = AsyncMock()
            worker._handle_interact_word_event = AsyncMock()

            for payload in (
                {"cmd": "WATCHED_CHANGE", "data": {"num": 123}},
                {"cmd": "HEARTBEAT_REPLY", "data": {"heartbeat": 1}},
                {"cmd": "READY", "data": {}},
                {"cmd": "SOME_TRANSPORT_EVENT", "data": {}},
            ):
                await worker._handle_event(payload)

            worker._handle_danmu_event.assert_not_awaited()
            worker._handle_gift_event.assert_not_awaited()
            worker._handle_superchat_event.assert_not_awaited()
            worker._handle_guard_event.assert_not_awaited()
            worker._handle_interact_word_event.assert_not_awaited()

        asyncio.run(scenario())


class BilibiliSystemEventTests(unittest.TestCase):
    def test_queued_gift_and_guard_entry_are_stamped_at_send_boundary(self):
        async def scenario():
            def build_adapter():
                adapter = BilibiliAdapter.__new__(BilibiliAdapter)
                adapter.config = SimpleNamespace(
                    platform="bilibili.live",
                    disable_command_trigger=False,
                    disable_video_sender_plugin=False,
                )
                adapter.logger = SimpleNamespace(info=Mock())
                adapter.tts_manager = SimpleNamespace(reset_idle_timer=Mock())
                adapter._get_template_info = AsyncMock(return_value=None)
                adapter._build_live_additional_config = Mock(return_value={"room_id": "100"})
                adapter.event_manager = SimpleNamespace(
                    gift_buffer={},
                    last_gift_time={},
                    push_to_event_queue=Mock(),
                )
                client = SimpleNamespace(send_message=AsyncMock())
                adapter.router = SimpleNamespace(
                    clients={"bilibili.live": client},
                    send_message=AsyncMock(),
                )
                return adapter, client

            gift_adapter, gift_client = build_adapter()
            await gift_adapter.handle_incoming_gift(
                room_id=100,
                gift_name="测试礼物",
                num=1,
                user_id="12345",
                user_name="测试用户",
                timestamp=100.0,
                price=20,
            )
            _, gift_message = gift_adapter.event_manager.push_to_event_queue.call_args.args
            with patch.object(adapter_module.time, "time", return_value=1000.0):
                await gift_adapter._send_to_nachobot(gift_message)

            self.assertEqual(gift_message.message_info.time, 1000.0)
            self.assertEqual(
                gift_message.message_info.additional_config["system_event"]["data"]["occurred_at"],
                100.0,
            )
            gift_client.send_message.assert_awaited_once()

            entry_adapter, entry_client = build_adapter()
            await entry_adapter.handle_incoming_guard_entry(
                room_id=100,
                user_id="12345",
                user_name="测试用户",
                guard_level=3,
                timestamp=200.0,
            )
            _, entry_message = entry_adapter.event_manager.push_to_event_queue.call_args.args
            with patch.object(adapter_module.time, "time", return_value=2000.0):
                await entry_adapter._send_to_nachobot(entry_message)

            self.assertEqual(entry_message.message_info.time, 2000.0)
            self.assertEqual(
                entry_message.message_info.additional_config["system_event"]["data"]["occurred_at"],
                200.0,
            )
            entry_client.send_message.assert_awaited_once()

        asyncio.run(scenario())

    def test_debounced_gift_uses_flush_time_after_read_watermark(self):
        async def scenario():
            read_watermark = 900.0
            occurrence_timestamp = read_watermark - 100.0
            flushed_messages = []
            scheduled_tasks = []
            adapter = SimpleNamespace(
                _get_template_info=AsyncMock(return_value=None),
                _build_live_additional_config=Mock(return_value={"room_id": "100"}),
            )

            async def capture_message(message):
                flushed_messages.append(message)

            adapter._send_to_nachobot = capture_message
            manager = EventManager(
                config=SimpleNamespace(platform="bilibili.live"),
                logger=SimpleNamespace(info=Mock(), error=Mock()),
                adapter_ref=adapter,
            )
            key = (100, "12345", "测试礼物")
            manager.gift_buffer[key] = {
                "count": 2,
                "price": 10,
                "timestamp": occurrence_timestamp,
                "user_name": "测试用户",
            }
            manager.last_gift_time[key] = read_watermark - 3.0

            sleep_count = 0

            async def sleep_until_flushed(_seconds):
                nonlocal sleep_count
                sleep_count += 1
                if sleep_count > 1:
                    raise asyncio.CancelledError

            def schedule(coroutine):
                task = asyncio.get_running_loop().create_task(coroutine)
                scheduled_tasks.append(task)
                return task

            fake_asyncio = SimpleNamespace(
                sleep=sleep_until_flushed,
                create_task=schedule,
                CancelledError=asyncio.CancelledError,
            )
            fake_clock = SimpleNamespace(time=Mock(return_value=read_watermark + 50.0))
            with (
                patch.object(event_manager_module, "asyncio", fake_asyncio),
                patch.object(event_manager_module, "time", fake_clock),
            ):
                await manager.gift_flush_loop()
                await asyncio.gather(*scheduled_tasks)

            self.assertEqual(len(flushed_messages), 1)
            message = flushed_messages[0]
            self.assertLess(occurrence_timestamp, read_watermark)
            # HeartFlow starts its query at last_read_time; this emitted row
            # therefore remains inside the current unread window.
            self.assertGreater(message.message_info.time, read_watermark)
            self.assertEqual(
                message.message_info.additional_config["system_event"]["data"]["occurred_at"],
                occurrence_timestamp,
            )

        asyncio.run(scenario())

    def test_superchat_uses_sender_none_and_preserves_actor_metadata(self):
        async def scenario():
            adapter = BilibiliAdapter.__new__(BilibiliAdapter)
            adapter.config = SimpleNamespace(platform="bilibili.live")
            adapter.logger = SimpleNamespace(info=Mock())
            adapter.tts_manager = SimpleNamespace(reset_idle_timer=Mock())
            adapter._get_template_info = AsyncMock(return_value=None)
            adapter._build_live_additional_config = Mock(
                return_value={"room_id": "100", "platform_event": {"kind": "support", "amount": 30}}
            )

            captured = []

            async def capture_message(message):
                captured.append(message)

            adapter._send_to_nachobot = capture_message

            await adapter.handle_incoming_superchat(
                room_id=100,
                message_text="测试SC",
                price=30,
                user_id="12345",
                user_name="测试用户",
                timestamp=1234.5,
            )
            await asyncio.sleep(0)

            self.assertEqual(len(captured), 1)
            message = captured[0]
            self.assertIsNone(message.message_info.user_info)
            self.assertEqual(
                message.message_segment.data[1].data,
                "发送了超级弹幕(SC)：测试SC (价值 30 元)",
            )

            additional_config = message.message_info.additional_config
            self.assertIsInstance(additional_config, dict)
            event = additional_config["system_event"]
            self.assertEqual(event["version"], 1)
            self.assertEqual(event["type"], "bilibili.superchat")
            self.assertEqual(event["actor"], {"user_id": "12345", "name": "测试用户"})
            self.assertIsNone(event["target"])
            self.assertEqual(event["data"]["room_id"], "100")
            self.assertEqual(event["data"]["message"], "测试SC")
            self.assertEqual(event["data"]["price"], 30)

        asyncio.run(scenario())


    def test_gift_guard_and_guard_entry_keep_actor_out_of_event_body(self):
        async def scenario():
            def build_adapter():
                adapter = BilibiliAdapter.__new__(BilibiliAdapter)
                adapter.config = SimpleNamespace(platform="bilibili.live")
                adapter.logger = SimpleNamespace(info=Mock())
                adapter.tts_manager = SimpleNamespace(reset_idle_timer=Mock())
                adapter._get_template_info = AsyncMock(return_value=None)
                adapter._build_live_additional_config = Mock(return_value={"room_id": "100"})
                return adapter

            gift_adapter = build_adapter()
            gift_adapter.event_manager = SimpleNamespace(
                gift_buffer={},
                last_gift_time={},
                push_to_event_queue=Mock(),
            )
            await gift_adapter.handle_incoming_gift(
                room_id=100,
                gift_name="测试礼物",
                num=1,
                user_id="12345",
                user_name="测试用户",
                timestamp=1234.5,
                price=20,
            )
            gift_adapter.event_manager.push_to_event_queue.assert_called_once()
            gift_priority, gift_message = gift_adapter.event_manager.push_to_event_queue.call_args.args
            self.assertEqual(gift_priority, 20)
            self.assertIsNone(gift_message.message_info.user_info)
            self.assertEqual(gift_message.message_segment.data[1].data, "送出了 测试礼物 x1")
            gift_event = gift_message.message_info.additional_config["system_event"]
            self.assertEqual(gift_event["type"], "bilibili.gift")
            self.assertEqual(gift_event["actor"], {"user_id": "12345", "name": "测试用户"})

            guard_adapter = build_adapter()
            guard_captured = []

            async def capture_guard(message):
                guard_captured.append(message)

            guard_adapter._send_to_nachobot = capture_guard
            await guard_adapter.handle_incoming_guard(
                room_id=100,
                guard_name="舰长",
                num=1,
                user_id="12345",
                user_name="测试用户",
                timestamp=1234.5,
                guard_level=3,
                price=198,
            )
            await asyncio.sleep(0)
            self.assertEqual(len(guard_captured), 1)
            guard_message = guard_captured[0]
            self.assertIsNone(guard_message.message_info.user_info)
            self.assertEqual(guard_message.message_segment.data[1].data, "开通了 舰长 (1 个月)")
            guard_event = guard_message.message_info.additional_config["system_event"]
            self.assertEqual(guard_event["type"], "bilibili.guard_buy")
            self.assertEqual(guard_event["actor"], {"user_id": "12345", "name": "测试用户"})

            entry_adapter = build_adapter()
            entry_adapter.event_manager = SimpleNamespace(push_to_event_queue=Mock())
            await entry_adapter.handle_incoming_guard_entry(
                room_id=100,
                user_id="12345",
                user_name="测试用户",
                guard_level=3,
                timestamp=1234.5,
            )
            entry_adapter.event_manager.push_to_event_queue.assert_called_once()
            entry_priority, entry_message = entry_adapter.event_manager.push_to_event_queue.call_args.args
            self.assertEqual(entry_priority, 20)
            self.assertIsNone(entry_message.message_info.user_info)
            self.assertEqual(entry_message.message_segment.data, "以舰长身份进入了直播间")
            entry_event = entry_message.message_info.additional_config["system_event"]
            self.assertEqual(entry_event["type"], "bilibili.guard_entry")
            self.assertEqual(entry_event["actor"], {"user_id": "12345", "name": "测试用户"})

        asyncio.run(scenario())

    def test_poke_uses_sender_none_and_preserves_actor_metadata(self):
        async def scenario():
            adapter = BilibiliAdapter.__new__(BilibiliAdapter)
            adapter.config = SimpleNamespace(platform="bilibili.live")
            adapter.logger = SimpleNamespace(info=Mock())
            adapter.tts_manager = SimpleNamespace(reset_idle_timer=Mock())
            adapter._get_template_info = AsyncMock(return_value=None)
            adapter._build_live_additional_config = Mock(return_value={"room_id": "100"})
            adapter.event_manager = SimpleNamespace(push_to_event_queue=Mock())

            await adapter.handle_incoming_poke(
                room_id=100,
                user_id="12345",
                user_name="测试用户",
            )

            adapter.event_manager.push_to_event_queue.assert_called_once()
            priority, message = adapter.event_manager.push_to_event_queue.call_args.args
            self.assertEqual(priority, 20)
            self.assertNotEqual(message.message_info.message_id, "notice")
            self.assertIsNone(message.message_info.user_info)

            additional_config = message.message_info.additional_config
            self.assertIsInstance(additional_config, dict)
            event = additional_config["system_event"]
            self.assertEqual(event["version"], 1)
            self.assertEqual(event["type"], "bilibili.poke")
            self.assertEqual(event["actor"], {"user_id": "12345", "name": "测试用户"})
            self.assertIsNone(event["target"])
            self.assertEqual(event["data"]["room_id"], "100")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
