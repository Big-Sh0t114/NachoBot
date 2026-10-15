"""BrainChat streaming replies keep expression learning off the foreground path."""

import asyncio
import json
import time
import unittest
from contextlib import ExitStack, asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ncnk_message import build_system_event
from src.chat.planner_actions.planner import ActionPlanner  # noqa: F401  # Prime plugin types.
from src.chat.replyer.group_generator import DefaultReplyer  # noqa: F401
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.express.expression_learner import ExpressionLearner
from src.chat.focus.models import FocusLease, FocusTurn, WakeReason
from src.chat.runtime_capabilities import classify_runtime_message_batch
from src.common.data_models.database_data_model import DatabaseMessages
import src.chat.brain_chat.brain_chat as brain_module
import src.chat.express.expression_learner as expression_learner_module


@asynccontextmanager
async def _prompt_scope(_template):
    yield


def _message(
    message_id: str,
    *,
    planner_bypass: bool = False,
    voice_stream: bool = False,
    platform: str = "arbitrary-platform",
    user_id: str | None = "speaker-1",
    extra_config: dict | None = None,
) -> DatabaseMessages:
    additional_config = dict(extra_config or {})
    additional_config["runtime_capabilities"] = {
        "schema_version": 1,
        "planner_bypass": planner_bypass,
        "voice_stream": voice_stream,
    }
    return DatabaseMessages(
        message_id=message_id,
        time=float(len(message_id)),
        chat_id="streaming-learning-test",
        processed_plain_text=f"safe synthetic text for {message_id}",
        display_message=f"safe synthetic text for {message_id}",
        additional_config=json.dumps(additional_config),
        user_id=user_id or "",
        user_nickname="Speaker" if user_id else "",
        user_platform=platform if user_id else "",
        chat_info_stream_id="streaming-learning-test",
        chat_info_platform=platform,
    )


def _focus_turn(wake_reason: WakeReason, *, events: tuple = ()) -> FocusTurn:
    return FocusTurn(
        lease=FocusLease("streaming-learning-focus", "streaming-learning-test", 1, "turn-1"),
        wake_reason=wake_reason,
        read_after_row_id=0,
        read_through_row_id=1,
        events=events,
    )


def _runtime(learner):
    stream = SimpleNamespace(
        stream_id="streaming-learning-test",
        platform="arbitrary-platform",
        group_info=None,
        context=SimpleNamespace(get_template_name=Mock(return_value=None)),
    )
    runtime = object.__new__(BrainChatting)
    runtime.stream_id = stream.stream_id
    runtime.log_prefix = "[streaming-learning-test]"
    runtime.chat_stream = stream
    runtime._cycle_counter = 0
    runtime.expression_learner = learner
    runtime.action_modifier = SimpleNamespace(modify_actions=AsyncMock())
    runtime.action_manager = SimpleNamespace(get_using_actions=Mock(return_value={}))
    runtime.action_planner = SimpleNamespace(
        get_necessary_info=Mock(return_value=(False, None, {})),
        last_obs_time_mark=0.0,
        build_planner_prompt=AsyncMock(return_value=("planner prompt", [])),
        plan=AsyncMock(return_value=([], None)),
    )
    runtime.last_read_time = 0.0
    runtime.blocked_users = {}
    runtime._planner_interrupt_flag = None
    runtime._planner_interrupt_requested = False
    runtime._planner_interrupt_consecutive_count = 0
    runtime._focus_turn_interrupted = False
    runtime._offline_recovery_batch = None
    runtime._offline_recovery_messages = []
    runtime._offline_recovery_prompt = ""
    runtime._offline_recovery_delivered = False
    runtime._offline_recovery_dropped = False
    runtime.start_cycle = Mock(return_value=({}, "thinking-1"))
    runtime.end_cycle = Mock()
    runtime.print_cycle_info = Mock()
    delivery_context = object()
    runtime._focus_reply_context = Mock(return_value=delivery_context)
    runtime._send_and_store_reply = AsyncMock(
        return_value=({"loop_action_info": {"action_taken": True}}, "delivered", {})
    )
    return runtime, delivery_context


@contextmanager
def _patched_observe(runtime):
    module = brain_module
    with ExitStack() as stack:
        manager = stack.enter_context(patch.object(module, "get_chat_manager"))
        manager.return_value.get_stream.return_value = runtime.chat_stream
        stack.enter_context(patch.object(module.global_prompt_manager, "async_message_scope", _prompt_scope))
        stack.enter_context(
            patch.object(module.global_prompt_manager, "get_prompt_async", new=AsyncMock(return_value="debug prompt"))
        )
        stack.enter_context(patch.object(module.advanced_manager, "is_on", return_value=False))
        stack.enter_context(patch.object(module.global_config.bot, "qq_account", "bot-account"))
        stack.enter_context(patch.object(module.global_config.chat, "get_max_context_size", return_value=30))
        stack.enter_context(patch.object(module, "get_stepped_limit", return_value=30))
        stack.enter_context(patch.object(module, "get_raw_msg_before_timestamp_with_chat", return_value=[]))
        stack.enter_context(
            patch.object(module, "build_readable_messages_with_id", side_effect=_render_messages)
        )
        stack.enter_context(patch.object(module.promise_cache_manager, "collect_snippets_for_messages", return_value=[]))
        stack.enter_context(
            patch.object(
                module.offline_recovery_manager,
                "filter_recovery_rows_from_context",
                side_effect=lambda _chat_id, rows: list(rows),
            )
        )
        stack.enter_context(
            patch.object(
                module.offline_recovery_manager,
                "filter_pending_recovery_messages",
                side_effect=lambda _chat_id, rows: list(rows),
            )
        )
        stack.enter_context(
            patch.object(
                module.realtime_ingress_manager,
                "filter_ordinary_messages",
                side_effect=lambda _chat_id, rows: list(rows),
            )
        )
        stack.enter_context(
            patch.object(
                module,
                "merge_planner_context_messages",
                side_effect=lambda _history, triggers, **_kwargs: list(triggers),
            )
        )
        stack.enter_context(patch.object(module.injection_manager, "build_injection_text", return_value=""))
        stack.enter_context(
            patch.object(
                module.events_manager,
                "handle_nacho_events",
                new=AsyncMock(return_value=(True, None)),
            )
        )
        stack.enter_context(
            patch(
                "src.memory_system.person_profile_injector.inject_person_profiles",
                new=AsyncMock(side_effect=lambda **kwargs: kwargs["chat_content_block"]),
            )
        )
        stack.enter_context(patch.object(module.focus_coordinator, "is_managed", return_value=False))
        async def generate_reply(**_kwargs):
            return True, SimpleNamespace(
                reply_set=object(),
                selected_expressions=None,
                context_refs=(),
                sandbox_edit_handoff=None,
            )

        stack.enter_context(patch.object(module.generator_api, "generate_reply", new=AsyncMock(side_effect=generate_reply)))
        yield stack


def _render_messages(messages, **_kwargs):
    return (
        "\n".join(message.processed_plain_text or "" for message in messages),
        [(message.message_id, message) for message in messages],
    )


class BrainChatStreamingLearningTests(unittest.TestCase):
    async def _assert_learning_blocks_before_cycle(
        self,
        messages,
        focus_turn,
        recovery_active,
        unauthorized_anonymous=False,
    ):
        entered = asyncio.Event()

        async def blocked_learning():
            entered.set()
            await asyncio.Event().wait()

        runtime, _ = _runtime(SimpleNamespace(trigger_learning_for_chat=AsyncMock(side_effect=blocked_learning)))
        if recovery_active:
            runtime._offline_recovery_batch = object()
        with _patched_observe(runtime), ExitStack() as patches:
            if unauthorized_anonymous:
                patches.enter_context(patch("src.chat.realtime_speaker.has_realtime_speaker_claim", return_value=True))
                patches.enter_context(
                    patch.object(
                        brain_module.realtime_ingress_manager,
                        "is_authorized_anonymous_speaker_message",
                        return_value=False,
                    )
                )
            task = asyncio.create_task(runtime._observe(recent_messages_list=messages, focus_turn=focus_turn))
            await asyncio.wait_for(entered.wait(), timeout=2)
            self.assertEqual(runtime.start_cycle.call_count, 0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        runtime.expression_learner.trigger_learning_for_chat.assert_awaited_once()
        runtime.start_cycle.assert_not_called()

    def test_streaming_batch_skips_learning_and_reaches_replyer_delivery_context(self):
        async def exercise():
            learner = SimpleNamespace(trigger_learning_for_chat=AsyncMock(side_effect=AssertionError("must be skipped")))
            runtime, delivery_context = _runtime(learner)
            messages = [
                _message("streaming-1", planner_bypass=True, voice_stream=True),
                _message("streaming-2", planner_bypass=True, voice_stream=True),
            ]
            with _patched_observe(runtime) as stack:
                generate_reply = stack.enter_context(patch.object(brain_module.generator_api, "generate_reply"))
                generate_reply.return_value = (
                    True,
                    SimpleNamespace(
                        reply_set=object(),
                        selected_expressions=None,
                        context_refs=(),
                        sandbox_edit_handoff=None,
                    ),
                )
                result = await runtime._observe(
                    recent_messages_list=messages,
                    focus_turn=_focus_turn(WakeReason.LOCAL_MESSAGE),
                )

            self.assertTrue(result)
            learner.trigger_learning_for_chat.assert_not_awaited()
            runtime.start_cycle.assert_called_once()
            runtime.action_planner.build_planner_prompt.assert_not_awaited()
            runtime.action_planner.plan.assert_not_awaited()
            self.assertEqual(generate_reply.await_count, len(messages))
            self.assertEqual(
                [call.kwargs["reply_message"] for call in generate_reply.await_args_list],
                messages,
            )
            for call in generate_reply.await_args_list:
                self.assertEqual(call.kwargs["request_type"], "replyer")
                self.assertIs(call.kwargs["reply_context"], delivery_context)
            self.assertEqual(runtime._send_and_store_reply.await_count, len(messages))
            self.assertEqual(
                [call.kwargs["action_message"] for call in runtime._send_and_store_reply.await_args_list],
                messages,
            )

        asyncio.run(exercise())

    def test_nonqualifying_turns_still_await_learning_before_cycle_start(self):
        ordinary = _message("ordinary-1")
        direct_nonstream = _message("direct-nonstream-1", planner_bypass=True)
        stream = _message("stream-2", planner_bypass=True, voice_stream=True)
        platform_event = _message(
            "platform-event-1",
            planner_bypass=True,
            voice_stream=True,
            extra_config={"platform_event": {"kind": "support", "amount": 1}},
        )
        system_event = _message(
            "system-event-1",
            planner_bypass=True,
            voice_stream=True,
            extra_config={
                "system_event": build_system_event(
                    "qq.poke",
                    actor={"user_id": "event-sender", "name": "Synthetic sender"},
                    target={"user_id": "bot-account", "name": "Bot"},
                )
            },
        )

        cases = (
            ("ordinary", [ordinary], None, False),
            ("direct nonstream", [direct_nonstream], None, False),
            ("mixed", [stream, ordinary], None, False),
            ("direct batch with a nonstream row", [stream, direct_nonstream], None, False),
            ("platform event", [platform_event], None, False),
            ("structured event", [system_event], None, False),
            ("offline recovery", [stream], None, True),
            ("Focus event", [stream], _focus_turn(WakeReason.FOCUS_EVENT, events=(object(),)), False),
            ("Focus event reason without snapshot", [stream], _focus_turn(WakeReason.FOCUS_EVENT), False),
        )

        for event_message in (platform_event, system_event):
            event_batch = classify_runtime_message_batch(
                [event_message],
                bot_user_id="bot-account",
                is_system_event=brain_module.get_system_event,
            )
            self.assertEqual(event_batch.direct_messages, (event_message,))
            self.assertEqual(event_batch.planner_messages, ())

        for name, messages, focus_turn, recovery_active in cases:
            with self.subTest(case=name):
                asyncio.run(self._assert_learning_blocks_before_cycle(messages, focus_turn, recovery_active))

    def test_unauthorized_anonymous_speaker_does_not_classify_as_direct(self):
        async def exercise():
            anonymous = _message(
                "anonymous-stream-1",
                planner_bypass=True,
                voice_stream=True,
                user_id=None,
            )
            anonymous.realtime_speaker = SimpleNamespace(claim_id="synthetic-claim")
            learner = SimpleNamespace(trigger_learning_for_chat=AsyncMock())
            runtime, _ = _runtime(learner)
            with (
                patch("src.chat.realtime_speaker.has_realtime_speaker_claim", return_value=True),
                patch.object(
                    brain_module.realtime_ingress_manager,
                    "is_authorized_anonymous_speaker_message",
                    return_value=False,
                ),
            ):
                batch = classify_runtime_message_batch(
                    [anonymous],
                    bot_user_id="bot-account",
                    is_system_event=brain_module.get_system_event,
                )
                self.assertEqual(batch.direct_messages, ())
                self.assertEqual(batch.planner_messages, (anonymous,))
                self.assertFalse(
                    runtime._should_skip_foreground_expression_learning(batch, [anonymous], None)
                )
                await self._assert_learning_blocks_before_cycle(
                    [anonymous],
                    None,
                    False,
                    unauthorized_anonymous=True,
                )

        asyncio.run(exercise())

    def test_focus_local_message_is_eligible_but_unknown_focus_shape_fails_closed(self):
        runtime, _ = _runtime(SimpleNamespace(trigger_learning_for_chat=AsyncMock()))
        message = _message("focus-local-1", planner_bypass=True, voice_stream=True)
        batch = classify_runtime_message_batch(
            [message],
            bot_user_id="bot-account",
            is_system_event=brain_module.get_system_event,
        )

        self.assertTrue(
            runtime._should_skip_foreground_expression_learning(
                batch,
                [message],
                _focus_turn(WakeReason.LOCAL_MESSAGE),
            )
        )
        self.assertFalse(
            runtime._should_skip_foreground_expression_learning(
                batch,
                [message],
                SimpleNamespace(wake_reason=WakeReason.LOCAL_MESSAGE),
            )
        )

    def test_skipped_streaming_turn_leaves_learning_watermark_and_rows_for_next_ordinary_trigger(self):
        async def exercise():
            learner = object.__new__(ExpressionLearner)
            learner.chat_id = "streaming-learning-test"
            learner.chat_name = "safe synthetic stream"
            learner.last_learning_time = time.time() - 900
            learner.min_messages_for_learning = 25
            learner.min_learning_interval = 300
            original_watermark = learner.last_learning_time
            retained_rows = [
                _message(f"retained-{index}")
                for index in range(25)
            ]
            row_snapshot = [
                (row.message_id, row.processed_plain_text, row.additional_config)
                for row in retained_rows
            ]
            query_rows = Mock(return_value=retained_rows)
            learner.learn_and_store = AsyncMock(return_value="learned synthetic style")
            runtime, _ = _runtime(learner)
            streaming_message = _message("streaming-3", planner_bypass=True, voice_stream=True)
            ordinary_message = _message("ordinary-2")

            with (
                patch.object(
                    brain_module.global_config.expression,
                    "get_expression_config_for_chat",
                    return_value=(False, True, 1.0),
                ),
                patch.object(
                    expression_learner_module,
                    "get_raw_msg_by_timestamp_with_chat_inclusive",
                    side_effect=query_rows,
                ),
                _patched_observe(runtime),
            ):
                await runtime._observe(recent_messages_list=[streaming_message])
                self.assertEqual(query_rows.call_count, 0)
                self.assertEqual(learner.last_learning_time, original_watermark)
                learner.learn_and_store.assert_not_awaited()

                await runtime._observe(recent_messages_list=[ordinary_message])

            query_rows.assert_called_once()
            self.assertEqual(query_rows.call_args.kwargs["timestamp_start"], original_watermark)
            learner.learn_and_store.assert_awaited_once_with(num=25)
            self.assertGreater(learner.last_learning_time, original_watermark)
            self.assertEqual(
                [
                    (row.message_id, row.processed_plain_text, row.additional_config)
                    for row in retained_rows
                ],
                row_snapshot,
            )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
