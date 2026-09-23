import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import adapter as adapter_module
from adapter import BilibiliAdapter
from bili_src.audio.tts_manager import TTSManager
from bili_src.core.runtime_profile import build_live_additional_config
import bili_src.live.event_manager as event_manager_module
from bili_src.live.event_manager import EventManager
from bili_src.live.live_worker import LiveRoomWorker


class _CoreClient:
    def __init__(self):
        self.calls = []

    async def synthesize_tts(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return {"audio_base64": base64.b64encode(text.encode("utf-8")).decode("ascii")}


class BiliTTSHotReloadTests(unittest.TestCase):
    def test_tts_without_search_still_uses_json_envelope_delivery(self):
        additional = build_live_additional_config(
            search_enabled=False,
            person_profile_enabled=False,
            tts_enabled=True,
            tts_language="ja",
        )

        capabilities = additional["runtime_capabilities"]
        self.assertEqual(capabilities["reply_delivery"], "json_envelope")
        self.assertEqual(capabilities["tts_language"], "ja")

    def test_enabled_room_prompt_requires_explicit_tts_field(self):
        async def scenario():
            adapter = BilibiliAdapter.__new__(BilibiliAdapter)
            adapter._get_cached_screen_summary = Mock(return_value=None)
            adapter._resolve_live_prompts = Mock(return_value=("base prompt", ""))
            adapter.tts_manager = SimpleNamespace(
                is_tts_enabled=Mock(return_value=True),
                get_room_language=Mock(return_value="ja"),
            )

            template = await adapter._get_template_info(100, "user", "hello")

            self.assertIsNotNone(template)
            prompt = template.template_items["replyer_prompt"]
            self.assertIn("`tts_text`", prompt)
            self.assertIn("`reply`只放给观众阅读的中文", prompt)
            self.assertNotIn("<JP>/<ZH>标签。\n<JP>", prompt)

        asyncio.run(scenario())

    def test_idle_segments_use_core_tts_boundary(self):
        async def scenario():
            client = _CoreClient()
            manager = TTSManager.__new__(TTSManager)
            manager.multimodal_client = client
            manager.config = SimpleNamespace(platform="bilibili.live")

            first_audio = await manager._synthesize_tts_segment(
                "idle-1", platform="bilibili.live", text_lang="ja"
            )
            second_audio = await manager._synthesize_tts_segment(
                "idle-2", platform="bilibili.live", text_lang="zh"
            )

            self.assertEqual(first_audio, b"idle-1")
            self.assertEqual(second_audio, b"idle-2")
            self.assertEqual([call[0] for call in client.calls], ["idle-1", "idle-2"])
            self.assertEqual(client.calls[0][1]["platform"], "bilibili.live")
            self.assertEqual(client.calls[0][1]["text_lang"], "ja")

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


class BilibiliV2ProtocolTests(unittest.TestCase):
    def test_v2_commands_are_dispatched_after_suffix_normalization(self):
        async def scenario():
            worker = LiveRoomWorker.__new__(LiveRoomWorker)
            worker.logger = SimpleNamespace(debug=Mock(), warning=Mock())
            worker._handle_danmu_event = AsyncMock()
            worker._handle_gift_event = AsyncMock()
            worker._handle_gift_v2_event = AsyncMock()
            worker._handle_superchat_event = AsyncMock()
            worker._handle_guard_event = AsyncMock()
            worker._handle_guard_v2_event = AsyncMock()
            worker._handle_interact_word_event = AsyncMock()

            await worker._handle_event({"cmd": "SEND_GIFT_V2:0:2", "data": {"pb": "x"}})
            await worker._handle_event({"cmd": "USER_TOAST_MSG_V2:extra", "data": {}})
            await worker._handle_event({"cmd": "SUPER_CHAT_MESSAGE:extra", "data": {}})
            await worker._handle_event({"cmd": "INTERACT_WORD_V2:extra", "data": {}})

            worker._handle_gift_v2_event.assert_awaited_once()
            worker._handle_guard_v2_event.assert_awaited_once()
            worker._handle_superchat_event.assert_awaited_once()
            worker._handle_interact_word_event.assert_awaited_once()

        asyncio.run(scenario())

    def test_send_gift_v2_forwards_every_gift_list_item(self):
        async def scenario():
            worker = LiveRoomWorker.__new__(LiveRoomWorker)
            worker.room_id = 100
            worker.logger = SimpleNamespace(info=Mock(), warning=Mock())
            worker.adapter = SimpleNamespace(handle_incoming_gift=AsyncMock())

            proto = SimpleNamespace(
                uid=12345,
                uname="测试用户",
                gift_list=[
                    SimpleNamespace(
                        gift_name="礼物A",
                        num=2,
                        total_coin=20000,
                        coin_type="gold",
                        price=10000,
                        timestamp=1000,
                        gift_id=1,
                        tid="tid-a",
                    ),
                    SimpleNamespace(
                        gift_name="礼物B",
                        num=1,
                        total_coin=0,
                        coin_type="gold",
                        price=5000,
                        timestamp=1001,
                        gift_id=2,
                        tid="tid-b",
                    ),
                ],
            )

            with patch(
                "bili_src.live.live_worker.SendGiftBroadcast.from_base64",
                return_value=proto,
            ):
                await worker._handle_gift_v2_event(
                    {"cmd": "SEND_GIFT_V2", "data": {"pb": "encoded"}},
                    "SEND_GIFT_V2",
                )

            self.assertEqual(worker.adapter.handle_incoming_gift.await_count, 2)
            first = worker.adapter.handle_incoming_gift.await_args_list[0].kwargs
            second = worker.adapter.handle_incoming_gift.await_args_list[1].kwargs

            self.assertEqual(first["gift_name"], "礼物A")
            self.assertEqual(first["num"], 2)
            self.assertEqual(first["price"], 10)
            self.assertEqual(first["user_id"], "12345")
            self.assertEqual(first["user_name"], "测试用户")
            self.assertEqual(first["timestamp"], 1000.0)

            self.assertEqual(second["gift_name"], "礼物B")
            self.assertEqual(second["num"], 1)
            self.assertEqual(second["price"], 5)
            self.assertEqual(second["timestamp"], 1001.0)

        asyncio.run(scenario())

    def test_guard_v2_and_guard_buy_are_deduplicated(self):
        async def scenario():
            worker = LiveRoomWorker.__new__(LiveRoomWorker)
            worker.room_id = 100
            worker._guard_event_seen = {}
            worker.logger = SimpleNamespace(info=Mock(), debug=Mock())
            worker.adapter = SimpleNamespace(handle_incoming_guard=AsyncMock())

            toast_payload = {
                "cmd": "USER_TOAST_MSG_V2",
                "data": {
                    "sender_uinfo": {"uid": 12345, "base": {"name": "测试用户"}},
                    "guard_info": {
                        "guard_level": 3,
                        "start_time": 1700000000,
                        "end_time": 1700000000,
                    },
                    "pay_info": {"num": 1, "price": 198000, "unit": "月"},
                    "gift_info": {"gift_id": 10003},
                    "option": {"source": 0},
                    "toast_msg": "测试用户开通了舰长",
                },
            }
            await worker._handle_guard_v2_event(toast_payload)

            guard_buy_payload = {
                "cmd": "GUARD_BUY",
                "data": {
                    "uid": 12345,
                    "username": "测试用户",
                    "guard_level": 3,
                    "num": 1,
                    "price": 198000,
                    "gift_name": "舰长",
                    "start_time": 1700000000,
                },
            }
            await worker._handle_guard_event(guard_buy_payload)

            # source=2 is the companion duplicate emitted by current Bilibili.
            source2_payload = {
                "cmd": "USER_TOAST_MSG_V2",
                "data": {
                    "sender_uinfo": {"uid": 54321, "base": {"name": "重复用户"}},
                    "guard_info": {"guard_level": 3, "start_time": 1700000001},
                    "pay_info": {"num": 1, "price": 198000, "unit": "月"},
                    "gift_info": {"gift_id": 10003},
                    "option": {"source": 2},
                },
            }
            await worker._handle_guard_v2_event(source2_payload)

            worker.adapter.handle_incoming_guard.assert_awaited_once()
            call = worker.adapter.handle_incoming_guard.await_args.kwargs
            self.assertEqual(call["user_id"], "12345")
            self.assertEqual(call["user_name"], "测试用户")
            self.assertEqual(call["guard_level"], 3)
            self.assertEqual(call["num"], 1)
            self.assertEqual(call["price"], 198)
            self.assertEqual(call["timestamp"], 1700000000.0)

        asyncio.run(scenario())

    def test_interact_word_v2_uses_protobuf_model(self):
        async def scenario():
            worker = LiveRoomWorker.__new__(LiveRoomWorker)
            worker.room_id = 100
            worker.logger = SimpleNamespace(info=Mock(), warning=Mock())
            worker.adapter = SimpleNamespace(handle_incoming_guard_entry=AsyncMock())

            proto = SimpleNamespace(
                uid=12345,
                uname="测试舰长",
                msg_type=1,
                privilege_type=3,
                timestamp=1700000100,
            )
            with patch(
                "bili_src.live.live_worker.InteractWordV2.from_base64",
                return_value=proto,
            ):
                await worker._handle_interact_word_event(
                    {"cmd": "INTERACT_WORD_V2", "data": {"pb": "encoded"}},
                    "INTERACT_WORD_V2",
                )

            worker.adapter.handle_incoming_guard_entry.assert_awaited_once_with(
                room_id=100,
                user_id="12345",
                user_name="测试舰长",
                guard_level=3,
                timestamp=1700000100.0,
            )

        asyncio.run(scenario())


class BilibiliTestCommandV2Tests(unittest.TestCase):
    @staticmethod
    def _build_adapter():
        adapter = BilibiliAdapter.__new__(BilibiliAdapter)
        adapter.config = SimpleNamespace(
            dede_user_id="12345",
            screen_manual_user_ids=[],
        )
        adapter.api = SimpleNamespace()
        adapter.logger = SimpleNamespace(info=Mock(), error=Mock())
        adapter._send_danmu = AsyncMock()
        adapter.handle_incoming_gift = AsyncMock()
        adapter.handle_incoming_superchat = AsyncMock()
        adapter.handle_incoming_guard = AsyncMock()
        adapter.handle_incoming_danmu = AsyncMock()
        return adapter

    def test_v1_and_v2_direct_test_commands_are_supported(self):
        async def scenario():
            adapter = self._build_adapter()

            for command in (
                "#test_gift",
                "#test_sc 测试SC",
                "#test_sc_v2 测试SC V2",
                "#test_guard",
                "#guard_enable=[level:C,message:测试舰长弹幕]",
                "#guard_enable_v2=[level:C,message:测试舰长弹幕V2]",
            ):
                handled = await adapter._handle_test_command(
                    room_id=100,
                    user_id="12345",
                    text=command,
                    user_name="测试用户",
                )
                self.assertTrue(handled, command)

            adapter.handle_incoming_gift.assert_awaited_once()
            self.assertEqual(adapter.handle_incoming_superchat.await_count, 2)
            adapter.handle_incoming_guard.assert_awaited_once()
            self.assertEqual(adapter.handle_incoming_danmu.await_count, 2)

        asyncio.run(scenario())

    def test_protocol_v2_test_commands_use_live_worker_dispatch(self):
        async def scenario():
            adapter = self._build_adapter()
            payloads = []

            class FakeWorker:
                def __init__(self, *args, **kwargs):
                    pass

                async def _handle_event(self, payload):
                    payloads.append(payload)

            with patch.object(adapter_module, "LiveRoomWorker", FakeWorker):
                for command in (
                    "#test_gift_v2",
                    "#test_guard_v2",
                    "#guard_entry",
                    "#guard_entry_v2",
                ):
                    handled = await adapter._handle_test_command(
                        room_id=100,
                        user_id="12345",
                        text=command,
                        user_name="测试用户",
                    )
                    self.assertTrue(handled, command)

            self.assertEqual(
                [payload["cmd"] for payload in payloads],
                [
                    "SEND_GIFT_V2",
                    "USER_TOAST_MSG_V2",
                    "INTERACT_WORD",
                    "INTERACT_WORD_V2",
                ],
            )

            from bili_src.live.v2_models import InteractWordV2, SendGiftBroadcast

            gift = SendGiftBroadcast.from_base64(payloads[0]["data"]["pb"])
            self.assertEqual(gift.uid, 12345)
            self.assertEqual(gift.uname, "测试用户")
            self.assertEqual(len(gift.gift_list), 1)
            self.assertEqual(gift.gift_list[0].gift_name, "测试V2礼物(TestGiftV2)")

            toast = payloads[1]["data"]
            self.assertEqual(toast["sender_uinfo"]["uid"], 12345)
            self.assertEqual(toast["guard_info"]["guard_level"], 3)
            self.assertEqual(toast["option"]["source"], 0)

            entry_v1 = payloads[2]["data"]
            self.assertEqual(entry_v1["msg_type"], 1)
            self.assertEqual(entry_v1["privilege_type"], 3)

            entry_v2 = InteractWordV2.from_base64(payloads[3]["data"]["pb"])
            self.assertEqual(entry_v2.uid, 12345)
            self.assertEqual(entry_v2.uname, "测试用户")
            self.assertEqual(entry_v2.msg_type, 1)
            self.assertEqual(entry_v2.privilege_type, 3)

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
