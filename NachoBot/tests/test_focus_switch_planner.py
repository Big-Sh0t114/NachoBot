import inspect
import asyncio
import json
import unittest
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.chat.planner_actions.planner import ActionPlanner
from src.chat.message_receive.storage import MessageStorage
from src.chat.replyer.group_generator import DefaultReplyer
from src.chat.brain_chat.brain_planner import BrainPlanner
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.focus.coordinator import FocusCoordinator
from src.chat.focus.models import (
    ChatKind,
    FocusGroupDefinition,
    FocusMember,
    FocusStoppedError,
    StoredMessageRef,
    SwitchChatRequest,
    TurnOutcome,
    TurnStatus,
    WakeReason,
)
from src.chat.focus.switch_action import SwitchDisposition, classify_switch_result
from src.chat.replyer.prompt.replyer_prompt import init_replyer_prompt
from src.chat.utils.chat_message_builder import build_readable_messages
from src.common.data_models.database_data_model import DatabaseMessages
import src.chat.brain_chat.brain_chat as brain_chat_module
import src.chat.brain_chat.brain_planner as brain_planner_module
import src.chat.heart_flow.heartFC_chat as heart_chat_module
import src.chat.planner_actions.planner as action_planner_module
import src.plugin_system.apis.send_api as send_api_module
import src.plugin_system.apis.message_api as message_api_module


class _Message:
    def __init__(self, user_id="user-1", platform="qq", text="hello"):
        self.user_info = SimpleNamespace(user_id=user_id, platform=platform)
        self.processed_plain_text = text


class _PlannerTestMixin:
    def _message(self):
        return _Message()

    def _planner(self, planner_type):
        planner = object.__new__(planner_type)
        planner.chat_id = "test-chat"
        planner.log_prefix = "[test-chat]"
        planner.last_obs_time_mark = 0.0
        planner.tool_executor = Mock()
        return planner


class FocusSwitchPlannerRegressionTests(_PlannerTestMixin, unittest.TestCase):
    def test_structured_system_event_wakes_active_focus_without_forcing_interrupt(self):
        async def exercise():
            coordinator = FocusCoordinator(unread_event_threshold=99)
            coordinator.register_group(
                FocusGroupDefinition(
                    group_id="group-system-event",
                    members=(
                        FocusMember("source", ChatKind.GROUP),
                        FocusMember("target", ChatKind.GROUP),
                    ),
                    initial_chat_id="source",
                )
            )

            ordinary = await coordinator.route_message(
                _Message(text="ordinary background activity"),
                StoredMessageRef(
                    row_id=1,
                    chat_id="target",
                    message_id="ordinary-1",
                    message_time=1.0,
                ),
            )
            self.assertTrue(ordinary.managed)
            self.assertFalse(ordinary.woke_active)
            self.assertIsNone(ordinary.event)

            system_message = _Message(text="送出了 测试礼物 x1")
            system_message.message_info = SimpleNamespace(
                additional_config={
                    "system_event": {
                        "version": 1,
                        "type": "bilibili.gift",
                        "actor": {"user_id": "12345", "name": "测试用户"},
                        "target": None,
                        "data": {"room_id": "100"},
                    }
                }
            )
            system_dispatch = await coordinator.route_message(
                system_message,
                StoredMessageRef(
                    row_id=2,
                    chat_id="target",
                    message_id="system-event-1",
                    message_time=2.0,
                ),
            )

            self.assertTrue(system_dispatch.managed)
            self.assertTrue(system_dispatch.woke_active)
            self.assertIsNotNone(system_dispatch.event)
            self.assertFalse(system_dispatch.interrupt_active)

        asyncio.run(exercise())

    def test_get_messages_by_time_in_chat_does_not_misroute_filter_command_to_filter_bot(self):
        system_event = SimpleNamespace(user_info=None)

        with (
            patch.object(
                message_api_module,
                "get_raw_msg_by_timestamp_with_chat",
                return_value=[system_event],
            ) as raw_query,
            patch.object(
                message_api_module,
                "filter_mai_messages",
                return_value=[system_event],
            ) as filter_mai,
        ):
            result = message_api_module.get_messages_by_time_in_chat(
                chat_id="1884227382",
                start_time=1.0,
                end_time=2.0,
                limit=20,
                limit_mode="latest",
                filter_mai=True,
                filter_command=True,
            )

        self.assertEqual(result, [system_event])
        raw_query.assert_called_once_with(
            "1884227382",
            1.0,
            2.0,
            20,
            "latest",
            filter_command=True,
        )
        filter_mai.assert_called_once_with([system_event])

    def test_filter_mai_keeps_senderless_system_event(self):
        bot_id = str(message_api_module.global_config.bot.qq_account)
        bot_message = SimpleNamespace(
            user_info=SimpleNamespace(user_id=bot_id),
            additional_config={},
        )
        user_message = SimpleNamespace(
            user_info=SimpleNamespace(user_id="other-user"),
            additional_config={},
        )
        system_event = SimpleNamespace(
            user_info=None,
            is_notify=True,
            additional_config={
                "system_event": {
                    "version": 1,
                    "type": "bilibili.poke",
                    "actor": {"user_id": "12345", "name": "测试用户"},
                    "target": None,
                    "data": {},
                }
            },
        )

        filtered = message_api_module.filter_mai_messages(
            [bot_message, system_event, user_message]
        )

        self.assertEqual(filtered, [system_event, user_message])

    def test_system_event_bypasses_silence_and_talk_threshold_before_planner(self):
        async def exercise():
            runtime = HeartFChatting.__new__(HeartFChatting)
            runtime.stream_id = "test-chat"
            runtime.last_read_time = 0.0
            runtime._last_message_received_at = 0.0
            runtime._planner_interrupt_flag = None
            runtime._planner_interrupt_requested = False
            runtime._planner_interrupt_consecutive_count = 0
            runtime._message_debounce_required = False
            runtime._focus_consumed_through_row_id = 0
            runtime.no_reply_until_call = True
            runtime.talk_threshold = 0.0

            message = SimpleNamespace(
                processed_plain_text="用鼠标戳了戳你",
                is_mentioned=False,
                is_at=False,
                additional_config={
                    "system_event": {
                        "version": 1,
                        "type": "bilibili.poke",
                        "actor": {"user_id": "12345", "name": "测试用户"},
                        "target": None,
                        "data": {"room_id": "100"},
                    }
                },
            )
            focus_turn = SimpleNamespace(
                read_after_row_id=0,
                read_through_row_id=1,
                events=(),
                handoff_ids=(),
                wake_reason=WakeReason.LOCAL_MESSAGE,
            )
            batch = SimpleNamespace(messages=(message,), consumed_through_row_id=1)

            runtime._filter_blocked_users = Mock(return_value=[message])
            runtime._observe = AsyncMock(return_value=True)

            with patch.object(heart_chat_module, "load_message_batch", return_value=batch):
                result = await runtime._loopbody(focus_turn)

            self.assertTrue(result)
            runtime._observe.assert_awaited_once_with(
                recent_messages_list=[message],
                focus_turn=focus_turn,
            )
            self.assertTrue(runtime.no_reply_until_call)

        asyncio.run(exercise())

    def test_false_switch_cas_fences_group_and_drops_stale_turn(self):
        class _FalseSwitchStateStore:
            def __init__(self):
                self.compare_calls = []
                self.commit_calls = []
                self.event_writes = []

            async def upsert_event(self, *args, **kwargs):
                self.event_writes.append((args, kwargs))

            async def compare_and_set_switch(self, **kwargs):
                self.compare_calls.append(kwargs)
                return False

            async def commit_turn(self, **kwargs):
                self.commit_calls.append(kwargs)

        async def exercise():
            state_store = _FalseSwitchStateStore()
            coordinator = FocusCoordinator(
                unread_event_threshold=1,
                state_store=state_store,
            )
            coordinator.register_group(
                FocusGroupDefinition(
                    group_id="group-1",
                    members=(
                        FocusMember("source", ChatKind.GROUP),
                        FocusMember("target", ChatKind.GROUP),
                    ),
                    initial_chat_id="source",
                )
            )

            dispatch = await coordinator.route_message(
                _Message(text="visible target event"),
                StoredMessageRef(
                    row_id=1,
                    chat_id="target",
                    message_id="message-1",
                    message_time=1.0,
                ),
            )
            self.assertTrue(dispatch.managed)
            self.assertTrue(dispatch.woke_active)
            self.assertIsNotNone(dispatch.event)

            turn = await coordinator.wait_for_turn("source")
            self.assertEqual(len(turn.events), 1)
            event = turn.events[0]
            result = await coordinator.switch_chat(
                SwitchChatRequest(
                    lease=turn.lease,
                    event_id=event.event_id,
                    expected_event_revision=event.revision,
                )
            )

            self.assertFalse(result.success)
            self.assertIn("switch compare-and-set failed", result.reason)
            self.assertIn("desynchronized", result.reason)
            self.assertEqual(classify_switch_result(result), SwitchDisposition.DROP)
            self.assertEqual(len(state_store.compare_calls), 1)
            self.assertFalse(await coordinator.is_current(turn.lease))

            finished = await coordinator.finish_turn(
                turn,
                TurnOutcome(
                    status=TurnStatus.COMPLETED,
                    delivered_event_revisions={event.event_id: event.revision},
                ),
            )
            self.assertFalse(finished)
            self.assertEqual(state_store.commit_calls, [])
            with self.assertRaises(FocusStoppedError):
                await coordinator.wait_for_turn("source")

        self._run_async(exercise())

    def test_heartflow_routes_switch_target_to_planner_and_requires_local_messages(self):
        source = inspect.getsource(HeartFChatting._observe)
        strategy_source = inspect.getsource(HeartFChatting._is_focus_switch_target_turn)

        self.assertNotIn("_focus_switch_target_reply_action", source)
        self.assertNotIn("switch_target_reply_action", source)
        self.assertIn("_is_focus_switch_target_turn", source)
        self.assertIn("focus_turn.wake_reason & WakeReason.SWITCH_TARGET", strategy_source)
        self.assertIn("and recent_messages_list", strategy_source)
        self.assertIn("allow_no_reply=allow_no_reply", source)

    def test_brainchat_passes_the_same_switch_target_policy_to_planner(self):
        source = inspect.getsource(BrainChatting._observe)
        strategy_source = inspect.getsource(BrainChatting._is_focus_switch_target_turn)

        self.assertIn("_is_focus_switch_target_turn", source)
        self.assertIn("focus_turn.wake_reason & WakeReason.SWITCH_TARGET", strategy_source)
        self.assertIn("and recent_messages_list", strategy_source)
        self.assertIn("allow_no_reply=allow_no_reply", source)

    def test_switch_target_strategy_keeps_named_shortcuts_at_higher_priority(self):
        focus_turn = SimpleNamespace(wake_reason=WakeReason.SWITCH_TARGET)
        message = self._message()

        self.assertTrue(HeartFChatting._is_focus_switch_target_turn(focus_turn, [message]))
        self.assertFalse(HeartFChatting._is_focus_switch_target_turn(focus_turn, [], planner_bypass=False))
        self.assertFalse(HeartFChatting._is_focus_switch_target_turn(focus_turn, [message], planner_bypass=True))
        self.assertTrue(HeartFChatting._should_use_notice_shortcut([message], True))
        self.assertFalse(HeartFChatting._should_use_notice_shortcut([], True))

        system_event_message = self._message()
        system_event_message.additional_config = {
            "system_event": {
                "version": 1,
                "type": "qq.poke",
                "actor": {"user_id": "user-1", "name": "Tester"},
                "target": None,
                "data": {},
            }
        }
        self.assertFalse(HeartFChatting._should_use_notice_shortcut([system_event_message], True))

        self.assertFalse(BrainChatting._is_focus_switch_target_turn(focus_turn, []))
        self.assertTrue(BrainChatting._is_focus_switch_target_turn(focus_turn, [message]))
        self.assertTrue(BrainChatting._should_use_advanced_direct_reply(True))
        self.assertFalse(BrainChatting._should_use_advanced_direct_reply(False))

    def test_system_event_database_round_trip_preserves_sender_none(self):
        additional_config = json.dumps(
            {
                "system_event": {
                    "version": 1,
                    "type": "qq.poke",
                    "actor": {"user_id": "123", "name": "甘油三酯"},
                    "target": {"user_id": "456", "name": "NachoBot"},
                    "data": {},
                }
            },
            ensure_ascii=False,
        )
        message = DatabaseMessages(
            message_id="evt-1",
            time=1.0,
            chat_id="qq_group_1",
            processed_plain_text="甘油三酯揉了揉NachoBot的脸",
            display_message="甘油三酯揉了揉NachoBot的脸",
            additional_config=additional_config,
            is_notify=True,
            chat_info_group_id="1",
            chat_info_group_name="测试群",
            chat_info_group_platform="qq",
            chat_info_stream_id="qq_group_1",
            chat_info_platform="qq",
        )

        self.assertIsNone(message.user_info)
        self.assertIsNone(message.chat_info.user_info)

        flattened = message.flatten()
        self.assertIsNone(flattened["user_id"])
        self.assertIsNone(flattened["user_nickname"])
        self.assertIsNone(flattened["user_platform"])
        self.assertIsNone(flattened["chat_info_user_id"])
        self.assertTrue(flattened["is_notify"])
        self.assertEqual(flattened["additional_config"], additional_config)


    def test_system_event_storage_uses_empty_chat_sender_placeholders(self):
        async def exercise():
            message = SimpleNamespace(
                processed_plain_text="揉了揉NachoBot的脸",
                interest_value=0.0,
                is_mentioned=False,
                is_at=False,
                reply_probability_boost=0.0,
                priority_mode="",
                priority_info={},
                is_emoji=False,
                is_picid=False,
                is_notify=True,
                is_command=False,
                key_words=[],
                key_words_lite=[],
                message_info=SimpleNamespace(
                    message_id="notice_1",
                    time=1.0,
                    user_info=None,
                    additional_config={
                        "system_event": {
                            "version": 1,
                            "type": "qq.poke",
                            "actor": {"user_id": "123", "name": "测试用户"},
                            "target": {"user_id": "456", "name": "NachoBot"},
                            "data": {},
                        }
                    },
                ),
            )
            chat_stream = SimpleNamespace(
                stream_id="qq_group_1",
                to_dict=lambda: {
                    "stream_id": "qq_group_1",
                    "platform": "qq",
                    "user_info": None,
                    "group_info": {
                        "platform": "qq",
                        "group_id": "1",
                        "group_name": "测试群",
                    },
                    "create_time": 1.0,
                    "last_active_time": 1.0,
                },
            )

            created = SimpleNamespace(id=1)
            with patch(
                "src.chat.message_receive.storage.Messages.create",
                return_value=created,
            ) as create_mock:
                await MessageStorage.store_message(message, chat_stream)

            kwargs = create_mock.call_args.kwargs
            self.assertEqual(kwargs["chat_info_user_platform"], "")
            self.assertEqual(kwargs["chat_info_user_id"], "")
            self.assertEqual(kwargs["chat_info_user_nickname"], "")
            self.assertIsNone(kwargs["chat_info_user_cardname"])
            self.assertIsNone(kwargs["user_platform"])
            self.assertIsNone(kwargs["user_id"])
            self.assertIsNone(kwargs["user_nickname"])
            self.assertTrue(kwargs["is_notify"])

        self._run_async(exercise())

    def test_system_event_readable_context_uses_system_event_prefix(self):
        additional_config = json.dumps(
            {
                "system_event": {
                    "version": 1,
                    "type": "qq.poke",
                    "actor": {"user_id": "123", "name": "甘油三酯"},
                    "target": {"user_id": "456", "name": "NachoBot"},
                    "data": {},
                }
            },
            ensure_ascii=False,
        )
        message = DatabaseMessages(
            message_id="evt-2",
            time=1.0,
            chat_id="qq_group_1",
            processed_plain_text="揉了揉NachoBot的脸",
            display_message="揉了揉NachoBot的脸",
            additional_config=additional_config,
            is_notify=True,
            chat_info_group_id="1",
            chat_info_group_name="测试群",
            chat_info_group_platform="qq",
            chat_info_stream_id="qq_group_1",
            chat_info_platform="qq",
        )

        rendered = build_readable_messages(
            [message],
            replace_bot_name=False,
            timestamp_mode="normal_no_YMD",
        )
        self.assertIn("[系统事件] 甘油三酯揉了揉NachoBot的脸", rendered)
        self.assertNotIn("甘油三酯: 甘油三酯揉了揉NachoBot的脸", rendered)


    def test_replyer_handles_senderless_system_event_without_user_semantics(self):
        async def exercise():
            replyer = DefaultReplyer.__new__(DefaultReplyer)
            replyer.request_type = "replyer"
            replyer.chat_stream = SimpleNamespace(
                stream_id="qq_group_1",
                platform="qq",
                user_info=None,
                group_info=SimpleNamespace(group_id="1", group_name="测试群"),
            )

            reply_message = DatabaseMessages(
                message_id="evt-reply-1",
                time=1.0,
                chat_id="qq_group_1",
                processed_plain_text="揉了揉NachoBot的脸",
                display_message="揉了揉NachoBot的脸",
                additional_config=json.dumps(
                    {
                        "system_event": {
                            "version": 1,
                            "type": "qq.poke",
                            "actor": {"user_id": "123", "name": "甘油三酯"},
                            "target": {"user_id": "456", "name": "NachoBot"},
                            "data": {},
                        }
                    },
                    ensure_ascii=False,
                ),
                is_notify=True,
                chat_info_group_id="1",
                chat_info_group_name="测试群",
                chat_info_group_platform="qq",
                chat_info_stream_id="qq_group_1",
                chat_info_platform="qq",
            )

            async def fake_timed(coro, name):
                result = await coro
                return name, result, 0.0

            captured_prompt_kwargs = {}

            async def fake_format_prompt(_template_name, **kwargs):
                captured_prompt_kwargs.update(kwargs)
                return "prompt"

            with (
                patch("src.chat.replyer.group_generator.get_latest_session_name", return_value=""),
                patch("src.chat.replyer.group_generator.get_raw_msg_before_timestamp_with_chat", return_value=[]),
                patch("src.chat.replyer.group_generator.build_readable_messages", return_value=""),
                patch("src.chat.replyer.group_generator.build_memory_retrieval_prompt", new=AsyncMock(return_value="")),
                patch("src.chat.replyer.group_generator.global_prompt_manager.format_prompt", new=fake_format_prompt),
                patch.object(replyer, "_time_and_run_task", side_effect=fake_timed),
                patch.object(replyer, "build_expression_habits", new=AsyncMock(return_value=("", []))),
                patch.object(replyer, "build_relation_info", new=AsyncMock(return_value="")),
                patch.object(replyer, "build_tool_info", new=AsyncMock(return_value="")),
                patch.object(replyer, "get_prompt_info", new=AsyncMock(return_value="")),
                patch.object(replyer, "build_actions_prompt", new=AsyncMock(return_value="")),
                patch.object(replyer, "build_personality_prompt", new=AsyncMock(return_value="")),
                patch.object(replyer, "_build_mid_term_memory_block", new=AsyncMock(return_value="")),
                patch.object(replyer, "build_keywords_reaction_prompt", new=AsyncMock(return_value="")),
                patch.object(replyer, "build_split_chat_history_prompts", return_value=("", "")),
                patch("src.chat.replyer.group_generator.global_config.mood.enable_mood", False),
            ):
                result = await DefaultReplyer.build_prompt_reply_context(replyer, reply_message=reply_message)

            self.assertEqual(result.prompt, "prompt")
            self.assertIn("现在发生了系统事件：甘油三酯揉了揉NachoBot的脸。引起了你的注意", captured_prompt_kwargs["reply_target_block"])
            self.assertNotIn("说的:", captured_prompt_kwargs["reply_target_block"])

        self._run_async(exercise())

    def test_disabled_pools_remove_silent_actions_but_defaults_keep_them(self):
        action_planner = self._planner(ActionPlanner)
        brain_planner = self._planner(BrainPlanner)

        action_pool = {"no_reply": object(), "no_reply_until_call": object(), "wait_time": object()}
        brain_pool = {"no_reply": object(), "wait_time": object()}

        self.assertEqual(
            set(ActionPlanner._without_silent_actions(action_pool, False)),
            {"wait_time"},
        )
        self.assertEqual(
            set(BrainPlanner._without_silent_actions(brain_pool, False)),
            {"wait_time"},
        )

        action = action_planner._parse_single_action(
            {"action": "no_reply"}, [("m1", self._message())], [], allow_no_reply=False
        )[0]
        action_until_call = action_planner._parse_single_action(
            {"action": "no_reply_until_call"}, [("m1", self._message())], [], allow_no_reply=False
        )[0]
        brain_action = brain_planner._parse_single_action(
            {"action": "no_reply"}, [("m1", self._message())], [], allow_no_reply=False
        )[0]

        self.assertEqual(action.action_type, "reply")
        self.assertEqual(action_until_call.action_type, "reply")
        self.assertEqual(brain_action.action_type, "reply")
        self.assertEqual(action.action_message.user_info.user_id, "user-1")
        self.assertEqual(brain_action.action_message.user_info.user_id, "user-1")

        self.assertEqual(
            action_planner._parse_single_action(
                {"action": "no_reply"}, [("m1", self._message())], [], allow_no_reply=True
            )[0].action_type,
            "no_reply",
        )
        self.assertEqual(
            brain_planner._parse_single_action(
                {"action": "no_reply"}, [("m1", self._message())], [], allow_no_reply=True
            )[0].action_type,
            "no_reply",
        )

    def test_disabled_request_empty_and_parse_fallbacks_are_replies(self):
        message = self._message()
        action_planner = self._planner(ActionPlanner)
        brain_planner = self._planner(BrainPlanner)

        for planner, module_name in (
            (action_planner, "src.chat.planner_actions.planner"),
            (brain_planner, "src.chat.brain_chat.brain_planner"),
        ):
            for response in (None, "", "not-json", RuntimeError("request failed")):
                llm = Mock()
                if isinstance(response, Exception):
                    llm.generate_response_async = AsyncMock(side_effect=response)
                else:
                    llm.generate_response_async = AsyncMock(return_value=(response, (None, None, None)))
                if isinstance(planner, ActionPlanner):
                    planner.planner_llm = llm
                else:
                    planner.separated_llm = llm

                with ExitStack() as stack:
                    if isinstance(planner, ActionPlanner):
                        stack.enter_context(patch(f"{module_name}.advanced_manager.is_on", return_value=False))
                    get_chat_manager = stack.enter_context(patch(f"{module_name}.get_chat_manager"))
                    get_chat_manager.return_value.get_stream.return_value = None
                    actions = self._run_async(
                        planner._execute_main_planner(
                            prompt="prompt",
                            message_id_list=[("m1", message)],
                            filtered_actions={"no_reply": object(), "wait_time": object()},
                            available_actions={"no_reply": object(), "wait_time": object()},
                            loop_start_time=0.0,
                            allow_no_reply=False,
                        )
                    )

                self.assertEqual([action.action_type for action in actions], ["reply"])
                self.assertIs(actions[0].action_message, message)
                self.assertNotIn("no_reply", actions[0].available_actions)

    def test_disabled_brain_raw_json_no_reply_becomes_latest_reply(self):
        older_message = _Message(user_id="user-1", text="older")
        latest_message = _Message(user_id="user-2", text="latest")
        brain_planner = self._planner(BrainPlanner)
        llm = Mock()
        llm.generate_response_async = AsyncMock(
            return_value=('{"action":"no_reply","target_message_id":"m1"}', (None, None, None))
        )
        brain_planner.separated_llm = llm

        actions = self._run_async(
            brain_planner._execute_main_planner(
                prompt="prompt",
                message_id_list=[("m1", older_message), ("m2", latest_message)],
                filtered_actions={},
                available_actions={},
                loop_start_time=0.0,
                allow_no_reply=False,
            )
        )

        self.assertEqual([action.action_type for action in actions], ["reply"])
        self.assertIs(actions[0].action_message, latest_message)

    def test_prompt_formatting_hides_only_disabled_silent_actions(self):
        action_planner = self._planner(ActionPlanner)
        brain_planner = self._planner(BrainPlanner)
        custom_silent_style = "保持沉默，不回复；请等待下一条消息。"
        init_replyer_prompt()

        async def build_action_prompt(allow_no_reply):
            with ExitStack() as stack:
                stack.enter_context(patch.object(action_planner_module, "get_actions_by_timestamp_with_chat", return_value=[]))
                stack.enter_context(patch.object(action_planner_module, "build_readable_actions", return_value=""))
                stack.enter_context(patch.object(action_planner, "_build_action_options_block", new=AsyncMock(return_value="")))
                stack.enter_context(patch.object(action_planner_module.advanced_manager, "is_on", return_value=False))
                stack.enter_context(patch.object(action_planner_module, "render_switch_planner_context", new=AsyncMock(return_value="")))
                stack.enter_context(
                    patch.object(action_planner_module.global_config.personality, "plan_style", custom_silent_style)
                )
                get_chat_manager = stack.enter_context(patch.object(action_planner_module, "get_chat_manager"))
                get_chat_manager.return_value.get_stream.return_value = SimpleNamespace()
                stack.enter_context(
                    patch(
                        "src.chat.heart_flow.appointment_scheduler.appointment_scheduler.get_pending",
                        return_value=[],
                    )
                )
                prompt, _ = await action_planner.build_planner_prompt(
                    is_group_chat=False,
                    chat_target_info=None,
                    current_available_actions={},
                    message_id_list=[],
                    chat_content_block="hello",
                    interest="",
                    allow_no_reply=allow_no_reply,
                )
                return prompt

        async def build_brain_prompt(allow_no_reply):
            with ExitStack() as stack:
                stack.enter_context(patch.object(brain_planner_module, "get_actions_by_timestamp_with_chat", return_value=[]))
                stack.enter_context(patch.object(brain_planner_module, "build_readable_actions", return_value=""))
                stack.enter_context(patch.object(brain_planner, "_build_action_options_block", new=AsyncMock(return_value="")))
                stack.enter_context(patch.object(brain_planner_module, "render_switch_planner_context", new=AsyncMock(return_value="")))
                stack.enter_context(
                    patch.object(
                        brain_planner_module.global_config.personality,
                        "private_plan_style",
                        custom_silent_style,
                    )
                )
                stack.enter_context(
                    patch(
                        "src.chat.heart_flow.appointment_scheduler.appointment_scheduler.get_pending",
                        return_value=[],
                    )
                )
                prompt, _ = await brain_planner.build_planner_prompt(
                    is_group_chat=False,
                    chat_target_info=None,
                    current_available_actions={},
                    message_id_list=[],
                    chat_content_block="hello",
                    interest="",
                    allow_no_reply=allow_no_reply,
                )
                return prompt

        async def build_brain_integrated_prompt(allow_no_reply):
            with ExitStack() as stack:
                stack.enter_context(patch.object(brain_planner_module, "get_actions_by_timestamp_with_chat", return_value=[]))
                stack.enter_context(patch.object(brain_planner_module, "build_readable_actions", return_value=""))
                stack.enter_context(patch.object(brain_planner, "_build_action_options_block", new=AsyncMock(return_value="")))
                stack.enter_context(patch.object(brain_planner_module, "get_stepped_limit", return_value=10))
                stack.enter_context(patch.object(brain_planner_module, "get_raw_msg_before_timestamp_with_chat", return_value=[]))
                stack.enter_context(patch.object(brain_planner_module, "build_readable_messages", return_value=""))
                stack.enter_context(patch.object(brain_planner_module, "build_relation_info", new=AsyncMock(return_value="relation")))
                stack.enter_context(
                    patch.object(
                        brain_planner_module,
                        "build_memory_retrieval_prompt",
                        new=AsyncMock(return_value="memory"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        brain_planner_module,
                        "build_lpmm_knowledge_info",
                        new=AsyncMock(return_value="knowledge"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        brain_planner_module.global_config.chat,
                        "get_max_context_size",
                        return_value=10,
                    )
                )
                stack.enter_context(
                    patch.object(
                        brain_planner_module.global_config.expression,
                        "get_expression_config_for_chat",
                        return_value=(False, None, None),
                    )
                )
                stack.enter_context(patch.object(brain_planner_module, "render_dynamic_prompt_template", return_value="persona"))
                get_chat_manager = stack.enter_context(patch.object(brain_planner_module, "get_chat_manager"))
                get_chat_manager.return_value.get_stream.return_value = SimpleNamespace()
                stack.enter_context(
                    patch.object(
                        brain_planner_module.global_config.personality,
                        "private_plan_style",
                        custom_silent_style,
                    )
                )
                prompt, _ = await brain_planner.build_integrated_planner_prompt(
                    is_group_chat=False,
                    chat_target_info=None,
                    current_available_actions={},
                    message_id_list=[],
                    chat_content_block="hello",
                    interest="",
                    allow_no_reply=allow_no_reply,
                )
                return prompt

        normal_action_prompt = self._run_async(build_action_prompt(True))
        forced_action_prompt = self._run_async(build_action_prompt(False))
        normal_brain_prompt = self._run_async(build_brain_prompt(True))
        forced_brain_prompt = self._run_async(build_brain_prompt(False))
        normal_integrated_prompt = self._run_async(build_brain_integrated_prompt(True))
        forced_integrated_prompt = self._run_async(build_brain_integrated_prompt(False))

        self.assertIn("no_reply", normal_action_prompt)
        self.assertIn("no_reply_until_call", normal_action_prompt)
        self.assertNotIn("no_reply", forced_action_prompt)
        self.assertNotIn("no_reply_until_call", forced_action_prompt)
        self.assertIn("no_reply", normal_brain_prompt)
        self.assertNotIn("no_reply", forced_brain_prompt)
        self.assertIn(custom_silent_style, normal_action_prompt)
        self.assertNotIn("保持沉默", forced_action_prompt)
        self.assertNotIn("不回复", forced_action_prompt)
        self.assertIn(custom_silent_style, normal_brain_prompt)
        self.assertNotIn("保持沉默", forced_brain_prompt)
        self.assertNotIn("不回复", forced_brain_prompt)
        self.assertIn(custom_silent_style, normal_integrated_prompt)
        self.assertIn("no_reply", normal_integrated_prompt)
        self.assertNotIn("no_reply", forced_integrated_prompt)
        self.assertNotIn("保持沉默", forced_integrated_prompt)
        self.assertNotIn("不回复", forced_integrated_prompt)
        self.assertIn("本轮必须从当前可用动作中选择一个有效动作。", forced_integrated_prompt)

    def test_disabled_brain_raw_json_mixed_array_keeps_valid_sibling(self):
        older_message = _Message(user_id="user-1", text="older")
        latest_message = _Message(user_id="user-2", text="latest")
        brain_planner = self._planner(BrainPlanner)
        llm = Mock()
        llm.generate_response_async = AsyncMock(
            return_value=(
                '[{"action":"unknown","target_message_id":"m1","extra":"drop","reason":"bad"},'
                '{"action":"reply","target_message_id":"m2","text":"keep","reason":"good"}]',
                (None, None, None),
            )
        )
        brain_planner.separated_llm = llm

        with patch.object(brain_planner_module, "has_active_focus_lease", return_value=False):
            actions = self._run_async(
                brain_planner._execute_main_planner(
                    prompt="prompt",
                    message_id_list=[("m1", older_message), ("m2", latest_message)],
                    filtered_actions={},
                    available_actions={},
                    loop_start_time=0.0,
                    allow_no_reply=False,
                )
            )

        self.assertEqual([action.action_type for action in actions], ["reply", "reply"])
        self.assertIs(actions[0].action_message, latest_message)
        self.assertEqual(actions[0].action_data, {"loop_start_time": 0.0})
        self.assertEqual(actions[1].action_message, latest_message)
        self.assertEqual(actions[1].reply_text, "keep")

    def test_disabled_silent_action_target_is_always_latest_user_message(self):
        older_message = _Message(user_id="user-1", text="older")
        latest_message = _Message(user_id="user-2", text="latest")
        message_id_list = [("m1", older_message), ("m2", latest_message)]

        action_planner = self._planner(ActionPlanner)
        for silent_action in ("no_reply", "no_reply_until_call"):
            action = action_planner._parse_single_action(
                {"action": silent_action, "target_message_id": "m1"},
                message_id_list,
                [],
                allow_no_reply=False,
            )[0]
            self.assertEqual(action.action_type, "reply")
            self.assertIs(action.action_message, latest_message)

        brain_planner = self._planner(BrainPlanner)
        action = brain_planner._parse_single_action(
            {"action": "no_reply", "target_message_id": "m1"},
            message_id_list,
            [],
            allow_no_reply=False,
        )[0]
        self.assertEqual(action.action_type, "reply")
        self.assertIs(action.action_message, latest_message)

    def test_disabled_invalid_action_target_is_always_latest_user_message(self):
        older_message = _Message(user_id="user-1", text="older")
        latest_message = _Message(user_id="user-2", text="latest")
        message_id_list = [("m1", older_message), ("m2", latest_message)]

        for planner_type, invalid_actions in (
            (ActionPlanner, ("no_action", "unknown")),
            (BrainPlanner, ("no_action", "unknown")),
        ):
            planner = self._planner(planner_type)
            for invalid_action in invalid_actions:
                action = planner._parse_single_action(
                    {
                        "action": invalid_action,
                        "target_message_id": "m1",
                        "unexpected": "must be discarded",
                    },
                    message_id_list,
                    [],
                    allow_no_reply=False,
                )[0]
                self.assertEqual(action.action_type, "reply")
                self.assertIs(action.action_message, latest_message)
                self.assertEqual(action.action_data, {})

    def test_disabled_policy_does_not_take_url_heuristic_shortcut(self):
        message = _Message(text="请看看 https://example.com")

        action_planner = self._planner(ActionPlanner)
        action_execute = AsyncMock(return_value=[])
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    action_planner,
                    "get_necessary_info",
                    return_value=(False, None, {}),
                )
            )
            stack.enter_context(patch.object(action_planner_module, "can_offer_switch_chat", return_value=False))
            stack.enter_context(
                patch.object(
                    action_planner_module,
                    "get_raw_msg_before_timestamp_with_chat",
                    return_value=[message],
                )
            )
            stack.enter_context(
                patch.object(
                    action_planner_module.global_config.chat,
                    "get_max_context_size",
                    return_value=10,
                )
            )
            stack.enter_context(patch.object(action_planner_module, "get_stepped_limit", return_value=10))
            stack.enter_context(
                patch.object(
                    action_planner_module,
                    "build_readable_messages_with_id",
                    return_value=("message", [("m1", message)]),
                )
            )
            stack.enter_context(
                patch.object(action_planner, "_filter_actions_by_activation_type", return_value={})
            )
            stack.enter_context(
                patch.object(
                    action_planner,
                    "build_planner_prompt",
                    new=AsyncMock(return_value=("prompt", [("m1", message)])),
                )
            )
            execute_mock = stack.enter_context(
                patch.object(action_planner, "_execute_main_planner", new=action_execute)
            )

            self._run_async(
                action_planner.plan(
                    available_actions={},
                    loop_start_time=0.0,
                    allow_no_reply=False,
                )
            )

        execute_mock.assert_awaited_once()
        self.assertFalse(execute_mock.await_args.kwargs["allow_no_reply"])

        brain_planner = self._planner(BrainPlanner)
        brain_execute = AsyncMock(return_value=[])
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    brain_planner,
                    "get_necessary_info",
                    return_value=(False, None, {}),
                )
            )
            stack.enter_context(
                patch.object(
                    brain_planner_module,
                    "get_raw_msg_before_timestamp_with_chat",
                    return_value=[message],
                )
            )
            stack.enter_context(
                patch.object(
                    brain_planner_module.global_config.chat,
                    "get_max_context_size",
                    return_value=10,
                )
            )
            stack.enter_context(patch.object(brain_planner_module, "get_stepped_limit", return_value=10))
            stack.enter_context(
                patch.object(
                    brain_planner_module,
                    "build_readable_messages_with_id",
                    return_value=("message", [("m1", message)]),
                )
            )
            stack.enter_context(
                patch.object(brain_planner, "_filter_actions_by_activation_type", return_value={})
            )
            stack.enter_context(
                patch.object(
                    brain_planner,
                    "build_planner_prompt",
                    new=AsyncMock(return_value=("prompt", [("m1", message)])),
                )
            )
            execute_mock = stack.enter_context(
                patch.object(brain_planner, "_execute_main_planner", new=brain_execute)
            )
            stack.enter_context(patch.object(brain_planner_module, "has_active_focus_lease", return_value=False))
            stack.enter_context(patch.object(brain_planner_module.global_config.bot, "integrated_plan", False))

            self._run_async(
                brain_planner.plan(
                    available_actions={},
                    loop_start_time=0.0,
                    allow_no_reply=False,
                )
            )

        execute_mock.assert_awaited_once()
        self.assertFalse(execute_mock.await_args.kwargs["allow_no_reply"])

    def test_heart_observe_switch_target_notice_shortcut_precedes_planner(self):
        class _ShortcutActionStop(RuntimeError):
            pass

        class _PlannerStop(RuntimeError):
            pass

        @asynccontextmanager
        async def prompt_scope(_template):
            yield

        message = _Message(text="notify target")
        message.is_notify = True
        focus_turn = SimpleNamespace(
            wake_reason=WakeReason.SWITCH_TARGET,
            events=[],
            handoff_ids=[],
            read_through_row_id=0,
            read_after_row_id=0,
            lease=SimpleNamespace(),
        )
        stream = SimpleNamespace(
            stream_id="test-chat",
            group_info=None,
            platform="qq",
            context=SimpleNamespace(get_template_name=Mock(return_value=None)),
        )
        runtime = object.__new__(HeartFChatting)
        runtime.stream_id = "test-chat"
        runtime.log_prefix = "[test-chat]"
        runtime.chat_stream = stream
        runtime._cycle_counter = 0
        runtime.expression_learner = SimpleNamespace(trigger_learning_for_chat=AsyncMock())
        runtime.action_modifier = SimpleNamespace(modify_actions=AsyncMock())
        runtime.action_manager = SimpleNamespace(get_using_actions=Mock(return_value={}))
        build_prompt_mock = AsyncMock(return_value=("prompt", [("m1", message)]))
        plan_mock = AsyncMock(side_effect=_PlannerStop("ordinary target reached Planner"))
        runtime.action_planner = SimpleNamespace(
            get_necessary_info=Mock(return_value=(False, None, {})),
            last_obs_time_mark=0.0,
            build_planner_prompt=build_prompt_mock,
            plan=plan_mock,
        )
        runtime.blocked_users = {}
        runtime.last_read_time = 0.0
        runtime._planner_interrupt_requested = False
        runtime._planner_interrupt_consecutive_count = 0
        runtime._planner_interrupt_flag = None
        runtime._focus_delivered_event_revisions = None
        executed_actions = []
        executed_targets = []

        async def execute_action(action, *_args):
            executed_actions.append(action.action_type)
            executed_targets.append(action.action_message)
            if action.action_type == "active_poke":
                raise _ShortcutActionStop("shortcut action boundary")
            return {
                "action_type": action.action_type,
                "success": True,
                "reply_text": "",
                "command": "",
                "terminal": False,
                "loop_info": {"loop_action_info": {"action_taken": True}},
            }

        with ExitStack() as stack:
            manager = stack.enter_context(patch.object(heart_chat_module, "get_chat_manager"))
            manager.return_value.get_stream.return_value = stream
            stack.enter_context(patch.object(heart_chat_module.global_prompt_manager, "async_message_scope", prompt_scope))
            prompt_fetch_mock = stack.enter_context(
                patch.object(
                    heart_chat_module.global_prompt_manager,
                    "get_prompt_async",
                    new=AsyncMock(return_value="debug prompt"),
                )
            )
            capabilities_mock = stack.enter_context(
                patch.object(
                    heart_chat_module,
                    "runtime_capabilities_from_stream",
                    return_value=SimpleNamespace(planner_bypass=False, notice_actions=True),
                )
            )
            stack.enter_context(patch.object(heart_chat_module.random, "random", return_value=0.1))
            stack.enter_context(patch.object(runtime, "start_cycle", return_value=({}, "thinking")))
            stack.enter_context(patch.object(runtime, "end_cycle", return_value=None))
            stack.enter_context(patch.object(runtime, "print_cycle_info", return_value=None))
            stack.enter_context(patch.object(runtime, "_execute_action", new=execute_action))
            stack.enter_context(patch.object(runtime, "_filter_blocked_users", return_value=[message]))
            stack.enter_context(patch.object(runtime, "_focus_forced_priority_action", return_value=None))
            stack.enter_context(patch.object(heart_chat_module, "get_stepped_limit", return_value=10))
            stack.enter_context(
                patch.object(
                    heart_chat_module,
                    "get_raw_msg_before_timestamp_with_chat",
                    return_value=[message],
                )
            )
            stack.enter_context(
                patch.object(
                    heart_chat_module,
                    "build_readable_messages_with_id",
                    return_value=("message", [("m1", message)]),
                )
            )
            stack.enter_context(
                patch.object(
                    heart_chat_module.promise_cache_manager,
                    "collect_snippets_for_messages",
                    return_value=[],
                )
            )
            stack.enter_context(
                patch(
                    "src.memory_system.heuristic_memory_injector.inject_memory_context",
                    new=AsyncMock(return_value="message"),
                )
            )
            stack.enter_context(
                patch(
                    "src.memory_system.person_profile_injector.inject_person_profiles",
                    new=AsyncMock(return_value="message"),
                )
            )
            stack.enter_context(
                patch.object(
                    heart_chat_module.global_config.chat,
                    "get_max_context_size",
                    return_value=10,
                )
            )
            stack.enter_context(
                patch.object(heart_chat_module.global_config.focus, "bypass_gate_enabled", False)
            )
            on_plan_mock = stack.enter_context(
                patch.object(
                    heart_chat_module.events_manager,
                    "handle_nacho_events",
                    new=AsyncMock(return_value=(True, None)),
                )
            )

            self._run_async(
                runtime._observe(
                    recent_messages_list=[message],
                    focus_turn=focus_turn,
                )
            )

            self.assertIn("active_poke", executed_actions)
            self.assertIn("no_reply", executed_actions)
            on_plan_mock.assert_not_awaited()
            build_prompt_mock.assert_not_awaited()
            plan_mock.assert_not_awaited()

            # 普通 switch-target 消息仍按原策略进入 Planner，且不能默认 no_reply。
            message.is_notify = False
            on_plan_mock.reset_mock()
            build_prompt_mock.reset_mock()
            plan_mock.reset_mock()
            with self.assertRaises(_PlannerStop):
                self._run_async(
                    runtime._observe(
                        recent_messages_list=[message],
                        focus_turn=focus_turn,
                    )
                )

            on_plan_mock.assert_awaited_once()
            build_prompt_mock.assert_awaited_once()
            self.assertFalse(build_prompt_mock.await_args.kwargs["allow_no_reply"])
            plan_mock.assert_awaited_once()
            self.assertFalse(plan_mock.await_args.kwargs["allow_no_reply"])

            # senderless structured system_event 不走 notice/direct-reply shortcut；
            # planner_bypass 会话直接由 Replyer 处理，不构建 Planner prompt，也不触发
            # ON_PLAN 或 Planner LLM。
            message.user_info = None
            message.is_notify = True
            message.additional_config = {
                "system_event": {
                    "version": 1,
                    "type": "qq.poke",
                    "actor": {"user_id": "user-1", "name": "Tester"},
                    "target": None,
                    "data": {},
                }
            }
            capabilities_mock.return_value = SimpleNamespace(planner_bypass=True, notice_actions=True)
            on_plan_mock.reset_mock()
            build_prompt_mock.reset_mock()
            plan_mock.reset_mock()

            self._run_async(
                runtime._observe(
                    recent_messages_list=[message],
                    focus_turn=focus_turn,
                )
            )

            on_plan_mock.assert_not_awaited()
            build_prompt_mock.assert_not_awaited()
            plan_mock.assert_not_awaited()
            prompt_fetch_mock.assert_not_awaited()
            self.assertEqual(executed_actions[-1], "reply")
            self.assertIs(executed_targets[-1], message)

    def test_system_event_normal_planner_and_bypass_replyer_for_both_batch_orders(self):
        class _PlannerReached(RuntimeError):
            pass

        @asynccontextmanager
        async def prompt_scope(_template):
            yield

        def make_event():
            return SimpleNamespace(
                message_id="event-1",
                time=2.0,
                user_info=None,
                processed_plain_text="系统事件内容",
                display_message="系统事件内容",
                is_mentioned=False,
                is_at=False,
                additional_config={
                    "system_event": {
                        "version": 1,
                        "type": "qq.poke",
                        "actor": {"user_id": "123", "name": "测试用户"},
                        "target": {"user_id": "999", "name": "NachoBot"},
                        "data": {},
                    }
                },
            )

        def make_ordinary():
            ordinary = _Message(user_id="ordinary", text="普通消息")
            ordinary.message_id = "ordinary-1"
            ordinary.time = 1.0
            ordinary.display_message = ordinary.processed_plain_text
            ordinary.additional_config = {}
            ordinary.is_mentioned = False
            ordinary.is_at = False
            return ordinary

        async def run_case(planner_bypass, ordered_messages):
            event_message = next(message for message in ordered_messages if message.user_info is None)
            stream = SimpleNamespace(
                stream_id="test-chat",
                group_info=SimpleNamespace(group_id="100", group_name="测试群"),
                platform="qq",
                user_info=None,
                context=SimpleNamespace(get_template_name=Mock(return_value=None)),
            )
            runtime = object.__new__(HeartFChatting)
            runtime.stream_id = "test-chat"
            runtime.log_prefix = "[test-chat]"
            runtime.chat_stream = stream
            runtime._cycle_counter = 0
            runtime.expression_learner = SimpleNamespace(trigger_learning_for_chat=AsyncMock())
            runtime.action_modifier = SimpleNamespace(modify_actions=AsyncMock())
            runtime.action_manager = SimpleNamespace(get_using_actions=Mock(return_value={}))
            runtime.last_read_time = 0.0
            runtime.blocked_users = {}
            runtime._planner_interrupt_requested = False
            runtime._planner_interrupt_consecutive_count = 0
            runtime._planner_interrupt_flag = None
            runtime._focus_delivered_event_revisions = None
            runtime.start_cycle = Mock(return_value=({}, "thinking"))
            runtime.end_cycle = Mock()
            runtime.print_cycle_info = Mock()
            runtime._filter_blocked_users = Mock(return_value=list(ordered_messages))

            build_prompt_mock = AsyncMock(return_value=("prompt", [("event-1", event_message)]))
            plan_mock = AsyncMock(side_effect=_PlannerReached("planner reached"))
            runtime.action_planner = SimpleNamespace(
                get_necessary_info=Mock(return_value=(True, None, {})),
                last_obs_time_mark=0.0,
                build_planner_prompt=build_prompt_mock,
                plan=plan_mock,
            )
            executed_actions = []

            async def execute_action(action, *_args):
                executed_actions.append(action)
                return {
                    "action_type": action.action_type,
                    "success": True,
                    "reply_text": "ok",
                    "command": "",
                    "terminal": False,
                    "loop_info": {"loop_action_info": {"action_taken": True}},
                }

            with ExitStack() as stack:
                manager = stack.enter_context(patch.object(heart_chat_module, "get_chat_manager"))
                manager.return_value.get_stream.return_value = stream
                stack.enter_context(
                    patch.object(heart_chat_module.global_prompt_manager, "async_message_scope", prompt_scope)
                )
                prompt_fetch_mock = stack.enter_context(
                    patch.object(
                        heart_chat_module.global_prompt_manager,
                        "get_prompt_async",
                        new=AsyncMock(return_value="debug prompt"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        heart_chat_module,
                        "runtime_capabilities_from_stream",
                        return_value=SimpleNamespace(planner_bypass=planner_bypass, notice_actions=True),
                    )
                )
                stack.enter_context(patch.object(runtime, "_execute_action", new=execute_action))
                stack.enter_context(patch.object(heart_chat_module, "get_stepped_limit", return_value=10))
                stack.enter_context(
                    patch.object(
                        heart_chat_module,
                        "get_raw_msg_before_timestamp_with_chat",
                        return_value=list(ordered_messages),
                    )
                )
                stack.enter_context(
                    patch.object(
                        heart_chat_module,
                        "build_readable_messages_with_id",
                        return_value=("message", [("event-1", event_message)]),
                    )
                )
                stack.enter_context(
                    patch.object(
                        heart_chat_module.promise_cache_manager,
                        "collect_snippets_for_messages",
                        return_value=[],
                    )
                )
                stack.enter_context(
                    patch(
                        "src.memory_system.heuristic_memory_injector.inject_memory_context",
                        new=AsyncMock(return_value="message"),
                    )
                )
                stack.enter_context(
                    patch(
                        "src.memory_system.person_profile_injector.inject_person_profiles",
                        new=AsyncMock(return_value="message"),
                    )
                )
                stack.enter_context(
                    patch.object(heart_chat_module.global_config.chat, "get_max_context_size", return_value=10)
                )
                stack.enter_context(patch.object(heart_chat_module.global_config.focus, "bypass_gate_enabled", False))
                on_plan_mock = stack.enter_context(
                    patch.object(
                        heart_chat_module.events_manager,
                        "handle_nacho_events",
                        new=AsyncMock(return_value=(True, None)),
                    )
                )

                if planner_bypass:
                    await runtime._observe(recent_messages_list=list(ordered_messages))
                    self.assertEqual(len(executed_actions), 1)
                    self.assertEqual(executed_actions[0].action_type, "reply")
                    self.assertIs(executed_actions[0].action_message, event_message)
                    self.assertIn("[系统事件]", executed_actions[0].action_data["bypass_extra_info"])
                    prompt_fetch_mock.assert_not_awaited()
                    build_prompt_mock.assert_not_awaited()
                    on_plan_mock.assert_not_awaited()
                    plan_mock.assert_not_awaited()
                else:
                    with self.assertRaises(_PlannerReached):
                        await runtime._observe(recent_messages_list=list(ordered_messages))
                    build_prompt_mock.assert_awaited_once()
                    self.assertTrue(build_prompt_mock.await_args.kwargs["allow_no_reply"])
                    on_plan_mock.assert_awaited_once()
                    plan_mock.assert_awaited_once()
                    self.assertTrue(plan_mock.await_args.kwargs["allow_no_reply"])

        async def exercise():
            for ordered_messages in ((make_event(), make_ordinary()), (make_ordinary(), make_event())):
                with self.subTest(planner_bypass=False, order=[m.message_id for m in ordered_messages]):
                    await run_case(False, ordered_messages)
                with self.subTest(planner_bypass=True, order=[m.message_id for m in ordered_messages]):
                    await run_case(True, ordered_messages)

        self._run_async(exercise())

    def test_brain_observe_switch_target_advanced_shortcut_precedes_planner(self):
        class _ShortcutActionStop(RuntimeError):
            pass

        class _PlannerStop(RuntimeError):
            pass

        @asynccontextmanager
        async def prompt_scope(_template):
            yield

        message = _Message(text="advanced target")
        focus_turn = SimpleNamespace(
            wake_reason=WakeReason.SWITCH_TARGET,
            events=[],
            handoff_ids=[],
            read_through_row_id=0,
            read_after_row_id=0,
        )
        stream = SimpleNamespace(
            stream_id="test-chat",
            group_info=None,
            context=SimpleNamespace(get_template_name=Mock(return_value=None)),
        )
        runtime = object.__new__(BrainChatting)
        runtime.stream_id = "test-chat"
        runtime.log_prefix = "[test-chat]"
        runtime.chat_stream = stream
        runtime._cycle_counter = 0
        runtime.expression_learner = SimpleNamespace(trigger_learning_for_chat=AsyncMock())
        runtime.action_modifier = SimpleNamespace(modify_actions=AsyncMock())
        runtime.action_manager = SimpleNamespace(get_using_actions=Mock(return_value={}))
        build_prompt_mock = AsyncMock(return_value=("prompt", [("m1", message)]))
        plan_mock = AsyncMock(side_effect=_PlannerStop("ordinary target reached Planner"))
        runtime.action_planner = SimpleNamespace(
            get_necessary_info=Mock(return_value=(False, None, {})),
            last_obs_time_mark=0.0,
            build_planner_prompt=build_prompt_mock,
            plan=plan_mock,
        )
        runtime.last_read_time = 0.0
        runtime._planner_interrupt_requested = False
        runtime._planner_interrupt_consecutive_count = 0
        runtime._planner_interrupt_flag = None
        execute_mock = AsyncMock(side_effect=_ShortcutActionStop("advanced direct-reply boundary"))

        with ExitStack() as stack:
            manager = stack.enter_context(patch.object(brain_chat_module, "get_chat_manager"))
            manager.return_value.get_stream.return_value = stream
            stack.enter_context(patch.object(brain_chat_module.global_prompt_manager, "async_message_scope", prompt_scope))
            stack.enter_context(
                patch.object(
                    brain_chat_module.global_prompt_manager,
                    "get_prompt_async",
                    new=AsyncMock(return_value="debug prompt"),
                )
            )
            advanced_mock = stack.enter_context(patch.object(brain_chat_module.advanced_manager, "is_on", return_value=True))
            stack.enter_context(patch.object(runtime, "start_cycle", return_value=({}, "thinking")))
            stack.enter_context(patch.object(runtime, "_execute_action", new=execute_mock))
            stack.enter_context(patch.object(brain_chat_module, "get_raw_msg_before_timestamp_with_chat", return_value=[message]))
            stack.enter_context(
                patch.object(
                    brain_chat_module,
                    "build_readable_messages_with_id",
                    return_value=("message", [("m1", message)]),
                )
            )
            stack.enter_context(
                patch.object(
                    brain_chat_module.promise_cache_manager,
                    "collect_snippets_for_messages",
                    return_value=[],
                )
            )
            stack.enter_context(
                patch.object(
                    brain_chat_module.global_config.chat,
                    "get_max_context_size",
                    return_value=10,
                )
            )
            stack.enter_context(
                patch(
                    "src.memory_system.person_profile_injector.inject_person_profiles",
                    new=AsyncMock(return_value="message"),
                )
            )
            on_plan_mock = stack.enter_context(
                patch.object(
                    brain_chat_module.events_manager,
                    "handle_nacho_events",
                    new=AsyncMock(return_value=(True, None)),
                )
            )

            with self.assertRaises(_ShortcutActionStop):
                self._run_async(
                    runtime._observe(
                        recent_messages_list=[message],
                        focus_turn=focus_turn,
                    )
                )

            execute_mock.assert_awaited_once()
            direct_action = execute_mock.await_args.args[0]
            self.assertEqual(direct_action.action_type, "reply")
            self.assertIs(direct_action.action_message, message)
            on_plan_mock.assert_not_awaited()
            build_prompt_mock.assert_not_awaited()
            plan_mock.assert_not_awaited()

            # Turning Advanced Mode off leaves this same local switch target on the
            # ordinary Planner path, with silent actions disabled for that turn.
            advanced_mock.return_value = False
            on_plan_mock.reset_mock()
            build_prompt_mock.reset_mock()
            plan_mock.reset_mock()
            with self.assertRaises(_PlannerStop):
                self._run_async(
                    runtime._observe(
                        recent_messages_list=[message],
                        focus_turn=focus_turn,
                    )
                )

            on_plan_mock.assert_awaited_once()
            build_prompt_mock.assert_awaited_once()
            self.assertFalse(build_prompt_mock.await_args.kwargs["allow_no_reply"])
            plan_mock.assert_awaited_once()
            self.assertFalse(plan_mock.await_args.kwargs["allow_no_reply"])

    def test_send_api_downgrades_senderless_system_event_native_reply(self):
        async def exercise():
            additional_config = json.dumps(
                {
                    "system_event": {
                        "version": 1,
                        "type": "qq.poke",
                        "actor": {"user_id": "123", "name": "甘油三酯"},
                        "target": {"user_id": "456", "name": "NachoBot"},
                        "data": {},
                    }
                },
                ensure_ascii=False,
            )
            reply_message = DatabaseMessages(
                message_id="evt-send-api",
                time=1.0,
                chat_id="qq_group_1",
                processed_plain_text="揉了揉NachoBot的脸",
                display_message="揉了揉NachoBot的脸",
                additional_config=additional_config,
                is_notify=True,
                chat_info_group_id="1",
                chat_info_group_name="测试群",
                chat_info_group_platform="qq",
                chat_info_stream_id="qq_group_1",
                chat_info_platform="qq",
            )

            rebuilt = send_api_module.db_message_to_message_recv(reply_message)
            self.assertIsNone(rebuilt.message_info.user_info)

            target_stream = SimpleNamespace(
                stream_id="qq_group_1",
                platform="qq",
                user_info=None,
                group_info=SimpleNamespace(group_id="1"),
            )
            sent_message = SimpleNamespace(message_info=SimpleNamespace(message_id="sent-1"))
            sender = SimpleNamespace(send_message=AsyncMock(return_value=sent_message))
            captured_message_sending = {}

            def fake_message_sending(**kwargs):
                captured_message_sending.update(kwargs)
                return SimpleNamespace(message_info=SimpleNamespace(additional_config={}))

            with (
                patch.object(
                    send_api_module,
                    "get_chat_manager",
                    return_value=SimpleNamespace(get_stream=lambda _stream_id: target_stream),
                ),
                patch.object(send_api_module, "UniversalMessageSender", return_value=sender),
                patch.object(send_api_module, "MessageSending", side_effect=fake_message_sending),
                patch.object(send_api_module, "_should_suppress_reply_by_policy", return_value=False),
            ):
                receipt = await send_api_module._send_to_target_receipt_permitted(
                    message_segment=send_api_module.Seg(type="text", data="回复系统事件"),
                    stream_id="qq_group_1",
                    display_message="回复系统事件",
                    set_reply=True,
                    reply_message=reply_message,
                    storage_message=False,
                    show_log=False,
                )

            self.assertEqual(receipt.status, send_api_module.SendStatus.DELIVERED)
            self.assertIsNone(captured_message_sending["reply"])
            self.assertEqual(captured_message_sending["reply_to"], "")
            sender.send_message.assert_awaited_once()
            self.assertFalse(sender.send_message.await_args.kwargs["set_reply"])

        self._run_async(exercise())

    @staticmethod
    def _run_async(awaitable):
        import asyncio

        return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
