import inspect
import unittest
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.chat.planner_actions.planner import ActionPlanner
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
import src.chat.brain_chat.brain_chat as brain_chat_module
import src.chat.brain_chat.brain_planner as brain_planner_module
import src.chat.heart_flow.heartFC_chat as heart_chat_module
import src.chat.planner_actions.planner as action_planner_module


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

        self.assertFalse(BrainChatting._is_focus_switch_target_turn(focus_turn, []))
        self.assertTrue(BrainChatting._is_focus_switch_target_turn(focus_turn, [message]))
        self.assertTrue(BrainChatting._should_use_advanced_direct_reply(True))
        self.assertFalse(BrainChatting._should_use_advanced_direct_reply(False))

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

        async def execute_action(action, *_args):
            executed_actions.append(action.action_type)
            if action.action_type == "active_poke":
                # The parallel shortcut action is intercepted before any real
                # adapter side effect; _observe intentionally gathers it.
                raise _ShortcutActionStop("shortcut action boundary")
            return {
                "action_type": action.action_type,
                "success": True,
                "reply_text": "",
                "command": "",
                "terminal": False,
            }

        with ExitStack() as stack:
            manager = stack.enter_context(patch.object(heart_chat_module, "get_chat_manager"))
            manager.return_value.get_stream.return_value = stream
            stack.enter_context(patch.object(heart_chat_module.global_prompt_manager, "async_message_scope", prompt_scope))
            stack.enter_context(
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

            # A real local message without notification eligibility is the ordinary
            # switch-target case and must continue through the Planner policy.
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

    @staticmethod
    def _run_async(awaitable):
        import asyncio

        return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
