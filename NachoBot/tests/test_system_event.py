import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from peewee import SqliteDatabase

from ncnk_message import (
    SystemEventState,
    build_system_event,
    build_system_event_route,
    classify_system_event,
    classify_system_event_route,
    get_system_event,
    get_system_event_route,
    system_event_fallback_text,
)


class SystemEventContractTests(unittest.TestCase):
    def test_fast_poke_is_registered_as_idempotent_bot_action_for_context(self):
        from src.chat.message_receive.bot import ChatBot
        from src.chat.utils.chat_message_builder import build_readable_actions, build_readable_messages
        from src.common.data_models.database_data_model import DatabaseActionRecords, DatabaseMessages
        from src.common.database.database_model import ActionRecords

        event = build_system_event(
            "qq.poke",
            actor={"user_id": "123", "name": "测试用户"},
            target={"user_id": "999", "name": "NachoBot"},
            data={"fast_poke": {"triggered": True, "result": "success"}},
        )
        message = SimpleNamespace(
            message_info=SimpleNamespace(
                message_id="notice-fast-poke-action",
                group_info=SimpleNamespace(group_id="test-group"),
                additional_config={"system_event": event},
            )
        )
        chat = SimpleNamespace(
            stream_id="qq-test-group",
            platform="qq",
            group_info=message.message_info.group_info,
        )

        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(str(Path(directory) / "fast-poke-action.db"))
            with database.bind_ctx([ActionRecords]):
                database.connect()
                database.create_tables([ActionRecords])
                try:
                    bot = ChatBot()
                    self.assertTrue(asyncio.run(bot._register_fast_poke_action(message, chat)))
                    # Reprocessing the same structured event updates the same action
                    # record instead of duplicating it in the chat context.
                    self.assertTrue(asyncio.run(bot._register_fast_poke_action(message, chat)))

                    records = list(ActionRecords.select())
                    self.assertEqual(len(records), 1)
                    record = records[0]
                    self.assertEqual(
                        record.action_id,
                        "system_event.fast_poke:qq-test-group:notice-fast-poke-action",
                    )
                    self.assertEqual(record.chat_id, "qq-test-group")
                    self.assertEqual(record.action_name, "active_poke")
                    self.assertTrue(record.action_done)
                    self.assertTrue(record.action_build_into_prompt)
                    self.assertEqual(record.action_prompt_display, "你快速回戳了测试用户")
                    action_data = json.loads(record.action_data)
                    self.assertEqual(action_data["result"], "success")
                    self.assertEqual(action_data["actor"]["user_id"], "123")
                    self.assertEqual(action_data["group_id"], "test-group")

                    readable = build_readable_actions(
                        [
                            DatabaseActionRecords(
                                action_id=record.action_id,
                                time=record.time,
                                action_name=record.action_name,
                                action_data=record.action_data,
                                action_done=record.action_done,
                                action_build_into_prompt=record.action_build_into_prompt,
                                action_prompt_display=record.action_prompt_display,
                                chat_id=record.chat_id,
                                chat_info_stream_id=record.chat_info_stream_id,
                                chat_info_platform=record.chat_info_platform,
                            )
                        ]
                    )
                    self.assertIn("你使用了“active_poke”", readable)
                    self.assertIn("你快速回戳了测试用户", readable)

                    replyer_context = build_readable_messages(
                        [
                            DatabaseMessages(
                                message_id="notice-fast-poke-action",
                                time=record.time - 0.1,
                                chat_id="qq-test-group",
                                processed_plain_text="测试用户戳了戳你",
                                chat_info_stream_id="qq-test-group",
                                chat_info_platform="qq",
                                chat_info_group_id="test-group",
                                chat_info_group_name="测试群",
                                chat_info_group_platform="qq",
                                additional_config=json.dumps(
                                    {"system_event": event},
                                    ensure_ascii=False,
                                ),
                                is_notify=True,
                            )
                        ],
                        show_actions=True,
                    )
                    self.assertIn("你快速回戳了测试用户", replyer_context)
                finally:
                    database.drop_tables([ActionRecords])
                    database.close()

    def test_untriggered_fast_poke_does_not_register_bot_action(self):
        from src.chat.message_receive.bot import ChatBot
        import src.chat.message_receive.bot as bot_module

        event = build_system_event(
            "qq.poke",
            actor={"user_id": "123", "name": "测试用户"},
            data={"fast_poke": {"triggered": False, "result": "skipped"}},
        )
        message = SimpleNamespace(
            message_info=SimpleNamespace(
                message_id="notice-fast-poke-skipped",
                group_info=SimpleNamespace(group_id="test-group"),
                additional_config={"system_event": event},
            )
        )
        wrong_type_message = SimpleNamespace(
            message_info=SimpleNamespace(
                message_id="notice-not-a-poke",
                group_info=SimpleNamespace(group_id="test-group"),
                additional_config={
                    "system_event": build_system_event(
                        "qq.group_admin.set",
                        data={"fast_poke": {"triggered": True, "result": "success"}},
                    )
                },
            )
        )
        chat = SimpleNamespace(stream_id="qq-test-group", platform="qq")

        async def exercise():
            bot = ChatBot()
            with patch.object(
                bot_module.database_api,
                "store_action_info",
                new=AsyncMock(),
            ) as store_action_info:
                registered = await bot._register_fast_poke_action(message, chat)
                wrong_type_registered = await bot._register_fast_poke_action(wrong_type_message, chat)
            return registered, wrong_type_registered, store_action_info

        registered, wrong_type_registered, store_action_info = asyncio.run(exercise())
        self.assertFalse(registered)
        self.assertFalse(wrong_type_registered)
        store_action_info.assert_not_awaited()

    def test_core_notice_log_reports_triggered_fast_poke(self):
        from src.chat.message_receive.bot import ChatBot
        import src.chat.message_receive.bot as bot_module

        event = {
            "version": 1,
            "type": "qq.poke",
            "actor": {"user_id": "123", "name": "测试用户"},
            "target": {"user_id": "999", "name": "NachoBot"},
            "data": {"fast_poke": {"triggered": True, "result": "success"}},
        }
        message = SimpleNamespace(
            message_info=SimpleNamespace(
                message_id="poke-log-test",
                additional_config={"system_event": event},
            ),
            is_notify=False,
        )

        async def exercise():
            bot = ChatBot()
            with patch.object(bot_module.logger, "info") as info_mock:
                handled = await bot.handle_notice_message(message)
            return handled, info_mock.call_args_list

        handled, calls = asyncio.run(exercise())
        self.assertTrue(handled)
        self.assertTrue(
            any(
                call.args[:1] == ("快速回戳事件: result=%s, message_id=%s",)
                and call.args[1:] == ("success", "poke-log-test")
                for call in calls
            )
        )

    def test_tri_state_and_persisted_json(self):
        self.assertEqual(classify_system_event({}).state, SystemEventState.ABSENT)
        self.assertEqual(
            classify_system_event({"runtime_capabilities": {}, "type": "ordinary"}).state,
            SystemEventState.ABSENT,
        )
        self.assertEqual(
            classify_system_event({"data": {"kind": "ordinary"}}).state,
            SystemEventState.ABSENT,
        )
        self.assertEqual(
            classify_system_event(
                SimpleNamespace(
                    message_info=SimpleNamespace(
                        additional_config={"type": "ordinary", "data": {"value": 1}}
                    )
                )
            ).state,
            SystemEventState.ABSENT,
        )

        event = build_system_event(
            "qq.poke",
            actor={"user_id": "123", "name": "测试用户"},
            target={"name": "NachoBot"},
        )
        self.assertEqual(classify_system_event(event).state, SystemEventState.VALID)
        persisted = json.dumps({"system_event": event}, ensure_ascii=False)
        self.assertEqual(get_system_event(persisted), event)
        self.assertEqual(classify_system_event(event).state, SystemEventState.VALID)

        invalid = {"system_event": {**event, "version": 2}}
        self.assertEqual(classify_system_event(invalid).state, SystemEventState.INVALID)
        self.assertEqual(
            classify_system_event({"version": 1, "type": "qq.notice", "unexpected": True}).state,
            SystemEventState.INVALID,
        )
        self.assertEqual(
            classify_system_event(
                {"additional_config": {"system_event": {"version": 1, "type": ""}}}
            ).state,
            SystemEventState.INVALID,
        )

    def test_builder_rejects_invalid_shapes_and_preserves_data(self):
        with self.assertRaises(ValueError):
            build_system_event("", actor={"user_id": "123"})
        with self.assertRaises(ValueError):
            build_system_event("qq.notice", actor={"name": ""})
        with self.assertRaises(ValueError):
            build_system_event("qq.notice", data=[])
        with self.assertRaises(ValueError):
            build_system_event("qq.notice", data={"value": float("nan")})

        event = build_system_event("qq.notice", data={"extra": [1, 2]})
        self.assertEqual(event["data"], {"extra": [1, 2]})
        self.assertEqual(system_event_fallback_text(event), "[系统事件: qq.notice]")

    def test_private_route_is_explicit_and_json_compatible(self):
        route = build_system_event_route(
            "qq",
            user_id=123,
            nickname="测试用户",
            cardname="群名片",
        )
        self.assertEqual(
            route,
            {
                "platform": "qq",
                "kind": "private",
                "peer": {
                    "user_id": "123",
                    "nickname": "测试用户",
                    "cardname": "群名片",
                },
            },
        )
        self.assertEqual(get_system_event_route({"system_event_route": route}), route)
        self.assertEqual(classify_system_event_route(route).state, SystemEventState.VALID)
        self.assertEqual(
            get_system_event_route(
                {"platform": "qq", "kind": "private", "user_id": "123", "name": "测试用户"}
            ),
            {
                "platform": "qq",
                "kind": "private",
                "peer": {"user_id": "123", "name": "测试用户"},
            },
        )
        for invalid in (
            None,
            {"platform": "qq", "kind": "group", "peer": {"user_id": "123"}},
            {"platform": "qq", "kind": "private", "peer": {"user_id": ""}},
            {"platform": "qq", "kind": "private", "peer": {"user_id": "123", "extra": True}},
            {"platform": "qq", "kind": "private", "peer": {"user_id": True}},
        ):
            self.assertEqual(classify_system_event_route({"system_event_route": invalid}).state, SystemEventState.INVALID)

    @staticmethod
    def _bot_message_data(
        event_config,
        *,
        user_info=None,
        sender_info=None,
        receiver_info=None,
        text="",
        group=True,
    ):
        return {
            "message_info": {
                "platform": "qq",
                "message_id": "system-event-test",
                "time": 1.0,
                "group_info": {
                    "platform": "qq",
                    "group_id": "test-group",
                    "group_name": "测试群",
                }
                if group
                else None,
                "user_info": user_info,
                "sender_info": sender_info,
                "receiver_info": receiver_info,
                "format_info": {"content_format": ["text"], "accept_format": ["text"]},
                "additional_config": event_config,
            },
            "message_segment": {"type": "text", "data": text},
            "raw_message": text,
        }

    def test_bot_drops_invalid_and_sender_bearing_events_before_user_hooks(self):
        """Present-invalid and sender-bearing envelopes never become ordinary messages."""
        from src.chat.message_receive.bot import ChatBot
        import src.chat.message_receive.bot as bot_module

        async def exercise():
            bot = ChatBot()
            bot._ensure_started = AsyncMock()
            receiver = AsyncMock()
            manager = SimpleNamespace(register_message=Mock(), get_or_create_stream=AsyncMock())
            touch_activity = Mock()

            with patch.object(bot_module, "get_chat_manager", return_value=manager), patch.object(
                bot_module.promise_cache_manager, "touch_activity", touch_activity
            ), patch.object(bot, "handle_notice_message", AsyncMock(return_value=False)), patch.object(
                bot_module.events_manager, "handle_nacho_events", AsyncMock()
            ), patch.object(bot_module, "track_platform_event", AsyncMock()), patch.object(
                bot_module.asyncio, "create_task", side_effect=lambda coroutine: coroutine.close()
            ), patch.object(bot.heartflow_message_receiver, "process_message", receiver):
                invalid = self._bot_message_data(
                    {"system_event": {"version": 1, "type": "", "actor": None, "target": None, "data": {}}}
                )
                sender_bearing = self._bot_message_data(
                    {"system_event": build_system_event("qq.notice", actor={"user_id": "actor"})},
                    user_info={"platform": "qq", "user_id": "ordinary", "user_nickname": "普通用户"},
                )

                await bot.message_process(invalid)
                await bot.message_process(sender_bearing)

            self.assertEqual(touch_activity.call_count, 0)
            self.assertEqual(manager.register_message.call_count, 0)
            self.assertEqual(receiver.await_count, 0)

        asyncio.run(exercise())

    def test_bot_keeps_empty_event_with_fallback_and_skips_user_hooks(self):
        """An empty rendered event is retained and routed with deterministic context text."""
        from src.chat.message_receive.bot import ChatBot
        import src.chat.message_receive.bot as bot_module

        event = build_system_event("qq.empty", actor={"name": "事件执行者"}, data={"value": 1})

        async def exercise():
            bot = ChatBot()
            bot._ensure_started = AsyncMock()
            receiver = AsyncMock()
            chat = SimpleNamespace(stream_id="qq-test-group", platform="qq", group_info=SimpleNamespace(group_name="测试群"))
            manager = SimpleNamespace(
                register_message=Mock(), get_or_create_stream=AsyncMock(return_value=chat)
            )
            touch_activity = Mock()
            promise_handler = Mock()
            preprocess = AsyncMock()
            on_message = AsyncMock()
            command_handler = AsyncMock()
            sandbox_callback = AsyncMock()
            ban_words = Mock(return_value=False)
            ban_regex = Mock(return_value=False)
            tracked = AsyncMock()
            fast_poke_action = AsyncMock(return_value=False)

            with patch.object(bot_module, "get_chat_manager", return_value=manager), patch.object(
                bot_module.promise_cache_manager, "touch_activity", touch_activity
            ), patch.object(bot_module.promise_cache_manager, "handle_message", promise_handler), patch.object(
                bot, "handle_notice_message", AsyncMock(return_value=True)
            ), patch.object(
                bot_module.events_manager, "handle_nacho_events", side_effect=[(True, None), (True, None)]
            ) as events, patch.object(bot_module, "track_platform_event", tracked), patch.object(
                bot.heartflow_message_receiver, "process_message", receiver
            ), patch.object(
                bot_module, "consume_sandbox_callback_reply", sandbox_callback
            ), patch.object(bot, "_process_commands_with_new_system", command_handler), patch.object(
                bot_module, "_check_ban_words", ban_words
            ), patch.object(bot_module, "_check_ban_regex", ban_regex), patch.object(
                bot, "_register_fast_poke_action", fast_poke_action
            ):
                payload = self._bot_message_data(
                    {"keep_me": {"untouched": True}, "system_event": event},
                    text="",
                )
                await bot.message_process(payload)
                # ``message_process`` schedules platform accounting as a task;
                # yield once so the scheduled coroutine actually runs before
                # asserting the awaited side effect.
                await asyncio.sleep(0)

            self.assertEqual(touch_activity.call_count, 0)
            self.assertEqual(promise_handler.call_count, 0)
            self.assertEqual(preprocess.await_count, 0)
            self.assertEqual(command_handler.await_count, 0)
            self.assertEqual(sandbox_callback.await_count, 0)
            self.assertEqual(ban_words.call_count, 0)
            self.assertEqual(ban_regex.call_count, 0)
            self.assertEqual(events.await_count, 0)
            manager.register_message.assert_called_once()
            manager.get_or_create_stream.assert_awaited_once()
            receiver.assert_awaited_once()
            routed_message = receiver.await_args.args[0]
            fast_poke_action.assert_awaited_once_with(routed_message, chat)
            self.assertIsNone(routed_message.message_info.user_info)
            self.assertEqual(routed_message.processed_plain_text, "[系统事件: qq.empty]")
            self.assertEqual(routed_message.message_info.additional_config["keep_me"], {"untouched": True})
            tracked.assert_awaited_once()

        asyncio.run(exercise())

    def test_platform_event_uses_senderless_actor_identity(self):
        from src.live import platform_event_tracker

        event = build_system_event("bilibili.gift", actor={"user_id": "actor-7", "name": "送礼用户"})
        message = SimpleNamespace(
            message_info=SimpleNamespace(
                platform="bilibili.live",
                user_info=None,
                additional_config={
                    "system_event": event,
                    "platform_event": {"kind": "support", "amount": 12.5},
                },
            )
        )
        person = Mock(is_known=True)

        async def exercise():
            with patch("src.person_info.person_info.Person", return_value=person) as person_ctor:
                await platform_event_tracker.track_platform_event(message)
            person_ctor.assert_called_once_with(platform="bilibili.live", user_id="actor-7")
            person.update_gift_value.assert_called_once_with(12.5)

        asyncio.run(exercise())
        self.assertIsNone(message.message_info.user_info)

    def test_senderless_event_is_kept_by_block_filter_and_history_builder(self):
        from src.chat.heart_flow.heartFC_chat import HeartFChatting
        from src.memory_system.chat_history_summarizer import ChatHistorySummarizer
        from src.common.data_models.database_data_model import DatabaseMessages

        event = build_system_event("qq.notice", actor={"name": "系统"})
        message = DatabaseMessages(
            message_id="event-db",
            time=1.0,
            chat_id="chat-db",
            processed_plain_text="[系统事件: qq.notice]",
            additional_config=json.dumps({"system_event": event}, ensure_ascii=False),
            chat_info_stream_id="chat-db",
            chat_info_platform="qq",
            chat_info_group_id="group-db",
            chat_info_group_name="测试群",
            chat_info_group_platform="qq",
        )
        heart_chat = HeartFChatting.__new__(HeartFChatting)
        heart_chat.log_prefix = "[chat-db]"
        heart_chat.blocked_users = {"unrelated-user": 9999999999.0}
        self.assertEqual(heart_chat._filter_blocked_users([message]), [message])
        self.assertIsNone(message.user_info)

        summarizer = ChatHistorySummarizer.__new__(ChatHistorySummarizer)
        numbered, _, _, participants = summarizer._build_numbered_messages_for_llm([message])
        self.assertIn("[系统事件]", numbered[0])
        self.assertEqual(participants[1], set())

    def test_private_event_requires_route_and_keeps_route_identity_out_of_sender(self):
        """Private structured events route by explicit peer metadata only."""
        from src.chat.message_receive.bot import ChatBot
        import src.chat.message_receive.bot as bot_module

        route = build_system_event_route("qq", user_id="peer-7", nickname="私聊用户")
        valid_event = build_system_event("qq.generic", actor={"user_id": "different-actor"})

        async def exercise():
            bot = ChatBot()
            bot._ensure_started = AsyncMock()
            receiver = AsyncMock()
            touch_activity = Mock()

            invalid_payloads = (
                self._bot_message_data(
                    {"system_event": valid_event},
                    group=False,
                    receiver_info={"user_info": {"platform": "qq", "user_id": "shortcut"}},
                ),
                self._bot_message_data(
                    {"system_event": valid_event, "system_event_route": {"platform": "qq"}},
                    group=False,
                ),
                self._bot_message_data(
                    {
                        "system_event": valid_event,
                        "system_event_route": {**route, "platform": "discord"},
                    },
                    group=False,
                ),
                self._bot_message_data(
                    {
                        "system_event": valid_event,
                        "system_event_route": route,
                    },
                    group=False,
                    sender_info={"user_info": {"platform": "qq", "user_id": "sender"}},
                ),
                self._bot_message_data(
                    {
                        "system_event": build_system_event("qq.poke", actor={"user_id": "different-actor"}),
                        "system_event_route": route,
                    },
                    group=False,
                ),
            )

            manager = SimpleNamespace(register_message=Mock(), get_or_create_stream=AsyncMock())
            with patch.object(bot_module, "get_chat_manager", return_value=manager), patch.object(
                bot_module.promise_cache_manager, "touch_activity", touch_activity
            ), patch.object(bot, "handle_notice_message", AsyncMock(return_value=True)), patch.object(
                bot_module, "track_platform_event", AsyncMock()
            ), patch.object(bot_module.asyncio, "create_task", side_effect=lambda coroutine: coroutine.close()), patch.object(
                bot.heartflow_message_receiver, "process_message", receiver
            ):
                for payload in invalid_payloads:
                    await bot.message_process(payload)

            self.assertEqual(manager.register_message.call_count, 0)
            self.assertEqual(receiver.await_count, 0)
            self.assertEqual(touch_activity.call_count, 0)

            chat = SimpleNamespace(
                stream_id="qq-private-peer-7",
                platform="qq",
                group_info=None,
            )
            manager = SimpleNamespace(
                register_message=Mock(),
                get_or_create_stream=AsyncMock(return_value=chat),
            )
            receiver.reset_mock()
            with patch.object(bot_module, "get_chat_manager", return_value=manager), patch.object(
                bot, "handle_notice_message", AsyncMock(return_value=True)
            ), patch.object(bot_module, "track_platform_event", AsyncMock()), patch.object(
                bot_module.asyncio, "create_task", side_effect=lambda coroutine: coroutine.close()
            ), patch.object(bot.heartflow_message_receiver, "process_message", receiver):
                await bot.message_process(
                    self._bot_message_data(
                        {"system_event": valid_event, "system_event_route": route},
                        group=False,
                        text="事件文本",
                    )
                )

            manager.register_message.assert_called_once()
            register_kwargs = manager.register_message.call_args.kwargs
            self.assertEqual(register_kwargs["routing_user_info"].user_id, "peer-7")
            manager.get_or_create_stream.assert_awaited_once()
            stream_kwargs = manager.get_or_create_stream.await_args.kwargs
            self.assertIsNone(stream_kwargs["user_info"])
            self.assertEqual(stream_kwargs["routing_user_info"].user_id, "peer-7")
            receiver.assert_awaited_once()
            routed_message = receiver.await_args.args[0]
            self.assertIsNone(routed_message.message_info.user_info)
            self.assertIsNone(routed_message.message_info.sender_info)
            self.assertEqual(
                routed_message.message_info.additional_config["system_event_route"],
                route,
            )

        asyncio.run(exercise())

    def test_unmanaged_heartflow_query_keeps_senderless_event_and_forces_observe(self):
        """The normal HeartFlow query seam must wake for a persisted event row."""
        from src.chat.heart_flow.heartFC_chat import HeartFChatting
        from src.common.database.database_model import Messages

        event = build_system_event("bilibili.gift", actor={"user_id": "actor-7", "name": "送礼用户"})

        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(str(Path(directory) / "heartflow-events.db"))
            with database.bind_ctx([Messages]):
                database.connect()
                database.create_tables([Messages])
                try:
                    Messages.create(
                        message_id="bilibili-event",
                        time=1.0,
                        chat_id="bilibili-stream",
                        chat_info_stream_id="bilibili-stream",
                        chat_info_platform="bilibili.live",
                        chat_info_user_platform="",
                        chat_info_user_id="",
                        chat_info_user_nickname="",
                        chat_info_create_time=1.0,
                        chat_info_last_active_time=1.0,
                        processed_plain_text="送礼用户送出了礼物",
                        additional_config=json.dumps({"system_event": event}, ensure_ascii=False),
                        is_command=False,
                        is_notify=True,
                    )

                    runtime = HeartFChatting.__new__(HeartFChatting)
                    runtime.stream_id = "bilibili-stream"
                    runtime.last_read_time = 0.0
                    runtime._last_message_received_at = 0.0
                    runtime._planner_interrupt_flag = None
                    runtime._planner_interrupt_requested = False
                    runtime._planner_interrupt_consecutive_count = 0
                    runtime._message_debounce_required = False
                    runtime._focus_consumed_through_row_id = 0
                    runtime.no_reply_until_call = True
                    runtime.talk_threshold = 0.0
                    runtime._filter_blocked_users = Mock(side_effect=lambda messages, **_: messages)
                    runtime._observe = AsyncMock(return_value=True)

                    result = asyncio.run(runtime._loopbody())

                    self.assertTrue(result)
                    runtime._observe.assert_awaited_once()
                    observed = runtime._observe.await_args.kwargs["recent_messages_list"]
                    self.assertEqual(len(observed), 1)
                    self.assertIsNone(observed[0].user_info)
                    self.assertEqual(get_system_event(observed[0])["type"], "bilibili.gift")
                finally:
                    database.drop_tables([Messages])
                    database.close()

    def test_unmanaged_heartflow_query_keeps_mentioned_group_message_and_excludes_commands(self):
        """A real command-filtered query must wake normal group chat for an @ message."""
        from src.chat.heart_flow.heartFC_chat import HeartFChatting
        from src.config.config import global_config
        from src.common.database.database_model import Messages

        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(str(Path(directory) / "heartflow-ordinary.db"))
            with database.bind_ctx([Messages]):
                database.connect()
                database.create_tables([Messages])
                try:
                    Messages.create(
                        message_id="ordinary-group",
                        time=1.0,
                        chat_id="qq-group",
                        chat_info_stream_id="qq-group",
                        chat_info_platform="qq",
                        chat_info_user_platform="qq",
                        chat_info_user_id="group-user",
                        chat_info_user_nickname="群友",
                        chat_info_create_time=1.0,
                        chat_info_last_active_time=1.0,
                        chat_info_group_platform="qq",
                        chat_info_group_id="group-1",
                        chat_info_group_name="测试群",
                        user_platform="qq",
                        user_id="group-user",
                        user_nickname="群友",
                        processed_plain_text="@NachoBot 请回复",
                        is_mentioned=True,
                        is_at=False,
                        is_command=False,
                    )
                    Messages.create(
                        message_id="command-group",
                        time=2.0,
                        chat_id="qq-group",
                        chat_info_stream_id="qq-group",
                        chat_info_platform="qq",
                        chat_info_user_platform="qq",
                        chat_info_user_id="group-user",
                        chat_info_user_nickname="群友",
                        chat_info_create_time=1.0,
                        chat_info_last_active_time=1.0,
                        chat_info_group_platform="qq",
                        chat_info_group_id="group-1",
                        chat_info_group_name="测试群",
                        user_platform="qq",
                        user_id="group-user",
                        user_nickname="群友",
                        processed_plain_text="/help",
                        is_mentioned=False,
                        is_at=False,
                        is_command=True,
                    )

                    runtime = HeartFChatting.__new__(HeartFChatting)
                    runtime.stream_id = "qq-group"
                    runtime.last_read_time = 0.0
                    runtime._last_message_received_at = 0.0
                    runtime._planner_interrupt_flag = None
                    runtime._planner_interrupt_requested = False
                    runtime._planner_interrupt_consecutive_count = 0
                    runtime._message_debounce_required = False
                    runtime._focus_consumed_through_row_id = 0
                    runtime.no_reply_until_call = True
                    runtime.talk_threshold = 0.0
                    runtime._filter_blocked_users = Mock(side_effect=lambda messages, **_: messages)
                    runtime._observe = AsyncMock(return_value=True)

                    with patch.object(global_config.chat, "mentioned_bot_reply", True):
                        result = asyncio.run(runtime._loopbody())

                    self.assertTrue(result)
                    runtime._observe.assert_awaited_once()
                    observed = runtime._observe.await_args.kwargs["recent_messages_list"]
                    self.assertEqual([message.message_id for message in observed], ["ordinary-group"])
                    self.assertTrue(observed[0].is_mentioned)
                    self.assertEqual(runtime._observe.await_args.kwargs["force_reply_message"], observed[0])
                    self.assertFalse(observed[0].is_command)
                finally:
                    database.drop_tables([Messages])
                    database.close()


if __name__ == "__main__":
    unittest.main()
