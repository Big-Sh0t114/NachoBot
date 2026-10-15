"""The private WebUI stream is handled by the real BrainChatting entry point."""

import asyncio
import json
import unittest
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.chat.planner_actions.planner import ActionPlanner  # Prime plugin types before generator imports.
from src.chat.replyer.group_generator import DefaultReplyer
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.chat.heart_flow.heartflow import Heartflow
from src.common.data_models.database_data_model import DatabaseMessages
from src.common.data_models.info_data_model import ActionPlannerInfo
import src.chat.brain_chat.brain_chat as brain_module
import src.chat.heart_flow.heartFC_chat as heart_chat_module
import src.chat.heart_flow.heartflow as heartflow_module


def _message(
    message_id,
    *,
    text="hello",
    platform="local",
    planner_bypass=...,
    source="webui-chat",
    channel=None,
    extra_config=None,
    user_id="webui-caller",
):
    config = dict(extra_config or {})
    if source is not None:
        config["source"] = source
    if channel is not None:
        config["webui_channel"] = channel
    if planner_bypass is not ...:
        config["runtime_capabilities"] = {
            "schema_version": 1,
            "planner_bypass": planner_bypass,
        }
    return DatabaseMessages(
        message_id=message_id,
        time=float(len(message_id)),
        chat_id="webui-private",
        processed_plain_text=text,
        display_message=text,
        additional_config=json.dumps(config, ensure_ascii=False),
        user_id=user_id or "",
        user_nickname="WebUI Caller" if user_id else "",
        user_platform=platform if user_id else "",
        chat_info_stream_id="webui-private",
        chat_info_platform=platform,
    )


def _platform_event(message_id="event-1", *, planner_bypass=...):
    return _message(
        message_id,
        text="platform event",
        user_id=None,
        planner_bypass=planner_bypass,
        extra_config={"platform_event": {"kind": "support", "amount": 1}},
    )


@asynccontextmanager
async def _prompt_scope(_template):
    yield


class BrainPrivateVoiceRoutingTests(unittest.TestCase):
    def _run_observe(
        self,
        unread_messages,
        history_messages,
        *,
        advanced=False,
        planner_actions=None,
        planner_error=None,
        plan_hook_continues=True,
    ):
        stream = SimpleNamespace(
            stream_id="webui-private",
            platform="local",
            group_info=None,
            context=SimpleNamespace(get_template_name=Mock(return_value=None)),
        )
        runtime = object.__new__(BrainChatting)
        runtime.stream_id = stream.stream_id
        runtime.log_prefix = "[webui-private]"
        runtime.chat_stream = stream
        runtime._cycle_counter = 0
        runtime.expression_learner = SimpleNamespace(trigger_learning_for_chat=AsyncMock())
        runtime.action_modifier = SimpleNamespace(modify_actions=AsyncMock())
        runtime.action_manager = SimpleNamespace(get_using_actions=Mock(return_value={}))
        plan_side_effect = planner_error
        planner = SimpleNamespace(
            get_necessary_info=Mock(return_value=(False, None, {})),
            last_obs_time_mark=0.0,
            build_planner_prompt=AsyncMock(return_value=("planner prompt", [])),
            plan=AsyncMock(
                side_effect=plan_side_effect,
                return_value=(list(planner_actions or []), None),
            ),
        )
        runtime.action_planner = planner
        runtime.last_read_time = 0.0
        runtime.blocked_users = {}
        runtime._planner_interrupt_flag = asyncio.Event()
        runtime._planner_interrupt_requested = True
        runtime._planner_interrupt_consecutive_count = 2
        runtime._focus_turn_interrupted = False
        runtime.start_cycle = Mock(return_value=({}, "thinking"))
        runtime.end_cycle = Mock()
        runtime.print_cycle_info = Mock()
        executed = []

        async def execute_action(action, *_args):
            executed.append(action)
            return {
                "action_type": action.action_type,
                "success": True,
                "reply_text": "",
                "loop_info": {"loop_action_info": {"action_taken": True}},
            }

        runtime._execute_action = AsyncMock(side_effect=execute_action)

        def render(messages, **_kwargs):
            return (
                "\n".join(message.processed_plain_text or "" for message in messages),
                [(message.message_id, message) for message in messages],
            )

        with ExitStack() as stack:
            manager = stack.enter_context(patch.object(brain_module, "get_chat_manager"))
            manager.return_value.get_stream.return_value = stream
            stack.enter_context(patch.object(brain_module.global_prompt_manager, "async_message_scope", _prompt_scope))
            stack.enter_context(
                patch.object(
                    brain_module.global_prompt_manager,
                    "get_prompt_async",
                    new=AsyncMock(return_value="debug prompt"),
                )
            )
            stack.enter_context(patch.object(brain_module.advanced_manager, "is_on", return_value=advanced))
            stack.enter_context(patch.object(brain_module, "get_raw_msg_before_timestamp_with_chat", return_value=list(history_messages)))
            stack.enter_context(patch.object(brain_module, "build_readable_messages_with_id", side_effect=render))
            stack.enter_context(
                patch.object(brain_module.promise_cache_manager, "collect_snippets_for_messages", return_value=[])
            )
            stack.enter_context(patch.object(brain_module.global_config.chat, "get_max_context_size", return_value=30))
            stack.enter_context(
                patch.object(
                    brain_module.events_manager,
                    "handle_nacho_events",
                    new=AsyncMock(return_value=(plan_hook_continues, None)),
                )
            )
            stack.enter_context(
                patch(
                    "src.memory_system.person_profile_injector.inject_person_profiles",
                    new=AsyncMock(side_effect=lambda **kwargs: kwargs["chat_content_block"]),
                )
            )

            runtime.observe_result = asyncio.run(runtime._observe(recent_messages_list=list(unread_messages)))

        return runtime, planner, executed

    def test_pure_voice_uses_real_brain_entry_with_and_without_reply_controls(self):
        for controls in (False, True):
            voice = _message("voice-1", text="recognized speech", channel="voice", planner_bypass=True)
            if controls:
                config = json.loads(voice.additional_config)
                config["runtime_capabilities"].update(
                    {
                        "reply_controls": True,
                        "control_emotions": ["happy", "calm"],
                        "control_actions": ["wave"],
                    }
                )
                voice.additional_config = json.dumps(config)

            runtime, planner, executed = self._run_observe([voice], [voice])

            with self.subTest(reply_controls=controls):
                planner.build_planner_prompt.assert_not_awaited()
                planner.plan.assert_not_awaited()
                self.assertEqual([action.action_type for action in executed], ["reply"])
                self.assertIs(executed[0].action_message, voice)
                self.assertIsNone(runtime._planner_interrupt_flag)
                self.assertFalse(runtime._planner_interrupt_requested)
                self.assertEqual(runtime._planner_interrupt_consecutive_count, 0)

    def test_text_after_voice_and_advanced_mode_explicit_false_still_use_planner(self):
        previous_voice = _message("voice-old", text="earlier transcript", channel="voice", planner_bypass=True)
        text = _message("text-1", text="typed follow-up", channel="text", planner_bypass=False)

        runtime, planner, executed = self._run_observe([text], [previous_voice, text], advanced=True)

        planner.build_planner_prompt.assert_awaited_once()
        target_messages = planner.build_planner_prompt.await_args.kwargs["message_id_list"]
        self.assertEqual([message for _, message in target_messages], [text])
        self.assertIn("earlier transcript", planner.build_planner_prompt.await_args.kwargs["chat_content_block"])
        self.assertIn("typed follow-up", planner.build_planner_prompt.await_args.kwargs["chat_content_block"])
        planner.plan.assert_awaited_once()
        self.assertIsInstance(planner.plan.await_args.kwargs["interrupt_flag"], asyncio.Event)
        self.assertEqual(executed, [])

    def test_mixed_batch_keeps_shared_context_but_direct_voice_is_not_a_planner_target(self):
        voice = _message("voice-1", text="voice transcript", channel="voice", planner_bypass=True)
        text = _message("text-1", text="typed request", channel="text", planner_bypass=False)
        planned_reply = ActionPlannerInfo(
            action_type="reply",
            reasoning="Planner reply",
            action_data={},
            action_message=text,
            available_actions={},
        )

        _, planner, executed = self._run_observe(
            [voice, text],
            [voice, text],
            planner_actions=[planned_reply],
        )

        planner.build_planner_prompt.assert_awaited_once()
        prompt = planner.build_planner_prompt.await_args.kwargs
        self.assertIn("voice transcript", prompt["chat_content_block"])
        self.assertIn("typed request", prompt["chat_content_block"])
        self.assertEqual([message for _, message in prompt["message_id_list"]], [text])
        planner.plan.assert_awaited_once()
        self.assertEqual([action.action_message for action in executed], [text, voice])

    def test_empty_or_event_only_turn_after_voice_does_not_inherit_direct_routing(self):
        prior_voice = _message("voice-old", text="old transcript", channel="voice", planner_bypass=True)
        event = _platform_event("event-new")

        _, empty_planner, empty_actions = self._run_observe([], [prior_voice])
        self.assertEqual(empty_actions, [])
        empty_planner.build_planner_prompt.assert_awaited_once()
        self.assertEqual(empty_planner.build_planner_prompt.await_args.kwargs["message_id_list"], [])
        empty_planner.plan.assert_awaited_once()

        _, event_planner, event_actions = self._run_observe([event], [prior_voice, event])
        self.assertEqual(event_actions, [])
        event_planner.build_planner_prompt.assert_awaited_once()
        self.assertEqual(
            [message for _, message in event_planner.build_planner_prompt.await_args.kwargs["message_id_list"]],
            [event],
        )
        event_planner.plan.assert_awaited_once()

    def test_qq_and_discord_voice_without_capability_keep_planner_routing(self):
        for platform in ("qq", "discord"):
            ordinary_voice = _message(
                f"{platform}-voice",
                platform=platform,
                text="ordinary platform transcript",
                channel="voice",
                planner_bypass=...,
                source=None,
            )
            _, planner, actions = self._run_observe([ordinary_voice], [ordinary_voice])
            with self.subTest(platform=platform):
                planner.build_planner_prompt.assert_awaited_once()
                planner.plan.assert_awaited_once()
                self.assertEqual(actions, [])

    def test_planner_failure_does_not_drop_direct_voice_action(self):
        class _PlannerFailure(RuntimeError):
            pass

        voice = _message("voice-1", text="voice transcript", channel="voice", planner_bypass=True)
        text = _message("text-1", text="typed request", channel="text", planner_bypass=False)

        _, planner, executed = self._run_observe(
            [voice, text],
            [voice, text],
            planner_error=_PlannerFailure("planner stopped"),
        )

        planner.plan.assert_awaited_once()
        self.assertEqual([action.action_message for action in executed], [voice])

    def test_on_plan_veto_preserves_direct_reply_in_mixed_batch(self):
        voice = _message("voice-1", text="voice transcript", channel="voice", planner_bypass=True)
        text = _message("text-1", text="typed request", channel="text", planner_bypass=False)

        _, planner, executed = self._run_observe(
            [voice, text],
            [voice, text],
            plan_hook_continues=False,
        )

        planner.build_planner_prompt.assert_awaited_once()
        planner.plan.assert_not_awaited()
        self.assertEqual([action.action_type for action in executed], ["reply"])
        self.assertIs(executed[0].action_message, voice)

    def test_on_plan_veto_without_direct_reply_still_returns_false(self):
        text = _message("text-1", text="typed request", channel="text", planner_bypass=False)

        runtime, planner, executed = self._run_observe(
            [text],
            [text],
            plan_hook_continues=False,
        )

        planner.build_planner_prompt.assert_awaited_once()
        planner.plan.assert_not_awaited()
        self.assertFalse(runtime.observe_result)
        self.assertEqual(executed, [])

    def test_private_factory_selects_brainchatting_for_local_voice_stream(self):
        voice = _message("voice-1", channel="voice", planner_bypass=True)
        stream = SimpleNamespace(
            stream_id="webui-private",
            platform="local",
            group_info=None,
            context=SimpleNamespace(message=voice),
        )
        flow = Heartflow(cleanup_interval_seconds=0)

        async def create():
            with ExitStack() as stack:
                manager = stack.enter_context(patch.object(heartflow_module, "get_chat_manager"))
                manager.return_value.get_stream.return_value = stream
                stack.enter_context(patch.object(BrainChatting, "__init__", return_value=None))
                start = stack.enter_context(patch.object(BrainChatting, "start", new=AsyncMock()))
                chat = await flow._create_heartflow_chat("webui-private")
            start.assert_awaited_once()
            self.assertIs(type(chat), BrainChatting)

        asyncio.run(create())

    def test_direct_reply_generation_uses_exact_trigger_and_ignores_stale_planner_interrupt(self):
        async def exercise():
            trigger = _message("voice-1", channel="voice", planner_bypass=True)
            runtime = object.__new__(BrainChatting)
            runtime.stream_id = "webui-private"
            runtime.log_prefix = "[webui-private]"
            runtime.chat_stream = SimpleNamespace(stream_id="webui-private")
            runtime._planner_interrupt_flag = asyncio.Event()
            runtime._planner_interrupt_flag.set()
            runtime._build_low_latency_person_profile_block = AsyncMock(return_value="")
            runtime._send_and_store_reply = AsyncMock(return_value=({}, "answer", []))
            runtime._focus_reply_context = Mock(return_value=None)
            action = ActionPlannerInfo(
                action_type="reply",
                reasoning="Adapter capability: direct reply",
                action_data={},
                action_message=trigger,
                available_actions={},
            )
            response = SimpleNamespace(
                reply_set=["answer"],
                selected_expressions=[],
                context_refs=[],
                sandbox_edit_handoff=None,
            )
            generate = AsyncMock(return_value=(True, response))
            with ExitStack() as stack:
                stack.enter_context(patch.object(brain_module.injection_manager, "build_injection_text", return_value=""))
                stack.enter_context(patch.object(brain_module.advanced_manager, "is_on", return_value=False))
                stack.enter_context(patch.object(brain_module.generator_api, "generate_reply", new=generate))
                result = await runtime._execute_action(action, [action], "thinking", {}, {})

            self.assertTrue(result["success"])
            self.assertIs(generate.await_args.kwargs["reply_message"], trigger)
            self.assertIsNone(generate.await_args.kwargs["interrupt_flag"])
            runtime._send_and_store_reply.assert_awaited_once()

        asyncio.run(exercise())


class HeartFlowPlannerInterruptTests(unittest.TestCase):
    def test_planned_reply_gets_live_interrupt_event_and_direct_reply_ignores_stale_event(self):
        planned_message = _message(
            "planned-text",
            text="ordinary group message",
            platform="qq",
            source=None,
            channel=None,
            planner_bypass=False,
        )
        direct_message = _message(
            "direct-trigger",
            text="direct reply trigger",
            platform="qq",
            source=None,
            channel=None,
            planner_bypass=True,
        )
        stream = SimpleNamespace(
            stream_id="webui-private",
            platform="qq",
            group_info=SimpleNamespace(group_id="test-group"),
            context=SimpleNamespace(get_template_name=Mock(return_value=None)),
        )
        runtime = object.__new__(HeartFChatting)
        runtime.stream_id = stream.stream_id
        runtime.log_prefix = "[heartflow-test]"
        runtime.chat_stream = stream
        runtime._cycle_counter = 0
        runtime.expression_learner = SimpleNamespace(trigger_learning_for_chat=AsyncMock())
        runtime.action_modifier = SimpleNamespace(modify_actions=AsyncMock())
        runtime.action_manager = SimpleNamespace(get_using_actions=Mock(return_value={}))
        runtime.action_planner = SimpleNamespace(
            get_necessary_info=Mock(return_value=(True, {}, None)),
            last_obs_time_mark=0.0,
            build_planner_prompt=AsyncMock(return_value=("planner prompt", [])),
        )
        planned_reply = ActionPlannerInfo(
            action_type="reply",
            reasoning="Planner reply",
            action_data={},
            action_message=planned_message,
            available_actions={},
        )
        stale_planner_flag = asyncio.Event()
        runtime._planner_interrupt_flag = stale_planner_flag
        runtime._planner_interrupt_requested = True
        runtime._planner_interrupt_consecutive_count = 0
        runtime._last_message_received_at = 0.0
        runtime._message_debounce_required = False
        runtime._focus_delivered_event_revisions = None
        runtime._focus_reply_context = Mock(return_value=None)
        runtime._build_low_latency_person_profile_block = AsyncMock(return_value="")
        runtime._send_and_store_reply = AsyncMock(
            return_value=({"loop_action_info": {"action_taken": True}}, "answer", [])
        )
        runtime.blocked_users = {}
        runtime.last_read_time = 0.0
        runtime.start_cycle = Mock(return_value=({}, "thinking"))
        runtime.end_cycle = Mock()
        runtime.print_cycle_info = Mock()

        planner_flags = []

        async def run_planner(**kwargs):
            self.assertNotIn("interrupt_flag", kwargs)
            planner_flags.append(runtime._planner_interrupt_flag)
            runtime.signal_new_message()
            return [planned_reply], None

        runtime.action_planner.plan = AsyncMock(side_effect=run_planner)
        history = [planned_message]

        def render(messages, **_kwargs):
            return (
                "\n".join(message.processed_plain_text or "" for message in messages),
                [(message.message_id, message) for message in messages],
            )

        response = SimpleNamespace(
            reply_set=["answer"],
            selected_expressions=[],
            context_refs=[],
            sandbox_edit_handoff=None,
        )
        generate = AsyncMock(return_value=(True, response))

        async def exercise():
            with ExitStack() as stack:
                manager = stack.enter_context(patch.object(heart_chat_module, "get_chat_manager"))
                manager.return_value.get_stream.return_value = stream
                stack.enter_context(
                    patch.object(heart_chat_module.global_prompt_manager, "async_message_scope", _prompt_scope)
                )
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
                        return_value=SimpleNamespace(notice_actions=False),
                    )
                )
                stack.enter_context(patch.object(heart_chat_module, "get_stepped_limit", return_value=10))
                stack.enter_context(
                    patch.object(
                        heart_chat_module,
                        "get_raw_msg_before_timestamp_with_chat",
                        side_effect=lambda **_kwargs: list(history),
                    )
                )
                stack.enter_context(
                    patch.object(heart_chat_module, "build_readable_messages_with_id", side_effect=render)
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
                    patch.object(
                        heart_chat_module.global_config.chat,
                        "planner_interrupt_enabled",
                        True,
                        create=True,
                    )
                )
                stack.enter_context(
                    patch.object(heart_chat_module.global_config.focus, "bypass_gate_enabled", False)
                )
                stack.enter_context(
                    patch.object(
                        heart_chat_module.events_manager,
                        "handle_nacho_events",
                        new=AsyncMock(return_value=(True, None)),
                    )
                )
                stack.enter_context(patch.object(heart_chat_module.injection_manager, "build_injection_text", return_value=""))
                stack.enter_context(patch.object(heart_chat_module.generator_api, "generate_reply", new=generate))

                await runtime._observe(recent_messages_list=[planned_message])

                self.assertIsNot(planner_flags[0], stale_planner_flag)
                self.assertIsInstance(planner_flags[0], asyncio.Event)
                self.assertTrue(planner_flags[0].is_set())
                self.assertIs(generate.await_args_list[0].kwargs["interrupt_flag"], planner_flags[0])
                self.assertIsNone(runtime._planner_interrupt_flag)

                stale_direct_flag = asyncio.Event()
                stale_direct_flag.set()
                runtime._planner_interrupt_flag = stale_direct_flag
                history[:] = [direct_message]
                await runtime._observe(recent_messages_list=[direct_message])

                runtime.action_planner.plan.assert_awaited_once()
                self.assertEqual(generate.await_count, 2)
                self.assertIsNone(generate.await_args_list[1].kwargs["interrupt_flag"])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
