from contextlib import ExitStack, asynccontextmanager
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.chat.planner_actions.planner import ActionPlanner
from src.chat.replyer.group_generator import DefaultReplyer  # Prime plugin API imports before BrainPlanner.
from src.chat.replyer.private_generator import PrivateReplyer
from src.chat.brain_chat.brain_planner import BrainPlanner
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.chat.message_receive.chat_stream import ChatMessageContext
from src.chat.planner_actions.action_modifier import ActionModifier
from src.chat.utils.planner_context import merge_planner_context_messages
from src.common.data_models.database_data_model import DatabaseMessages
from src.plugin_system.base.component_types import ActionInfo, ComponentType
import src.chat.brain_chat.brain_planner as brain_planner_module
import src.chat.planner_actions.planner as action_planner_module
import src.chat.replyer.group_generator as group_replyer_module
import src.chat.replyer.private_generator as private_replyer_module
import src.chat.utils.chat_message_builder as chat_message_builder_module
import src.chat.brain_chat.brain_chat as brain_chat_module
import src.chat.heart_flow.heartFC_chat as heart_chat_module


def _message(
    message_id,
    timestamp,
    *,
    chat_id="context-chat",
    row_id=None,
    user_id="sender-1",
    nickname="Sender",
    text="message",
    planner_bypass=False,
    held_recovery=False,
):
    additional_config = {"runtime_capabilities": {"planner_bypass": True}} if planner_bypass else {}
    message = DatabaseMessages(
        message_id=message_id,
        time=timestamp,
        chat_id=chat_id,
        processed_plain_text=text,
        display_message=text,
        user_id=user_id or "",
        user_nickname=nickname,
        user_platform="test",
        chat_info_stream_id=chat_id,
        chat_info_platform="test",
        additional_config=json.dumps(additional_config),
        is_command=False,
    )
    if row_id is not None:
        message.focus_row_id = row_id
    message.held_recovery = held_recovery
    return message


class _RecoveryFilter:
    @staticmethod
    def filter_pending_recovery_messages(_chat_id, messages):
        return [message for message in messages if not getattr(message, "held_recovery", False)]


class PlannerContextSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def test_merge_prefers_row_identity_and_reserves_wake_rows_within_limit(self):
        history = _message("shared-legacy-id", 10.0, row_id=1, text="history copy")
        wake = _message("shared-legacy-id", 11.0, row_id=2, text="wake row")
        old_wake = _message("old-wake", 5.0, row_id=5, text="old explicit wake")
        newer_one = _message("newer-one", 20.0, row_id=3)
        newer_two = _message("newer-two", 21.0, row_id=4)

        merged = merge_planner_context_messages(
            [history, newer_one, newer_two],
            [wake, old_wake],
            chat_id="context-chat",
            limit=2,
            history_before=22.0,
        )

        self.assertEqual([message.focus_row_id for message in merged], [5, 2])
        self.assertEqual([message.processed_plain_text for message in merged], ["old explicit wake", "wake row"])

    def test_merge_deduplicates_same_row_but_keeps_reused_ids_and_rejects_other_streams(self):
        history_copy = _message("same-id", 10.0, row_id=8, text="stale")
        snapshot_copy = _message("same-id", 10.0, row_id=8, text="snapshot with file preview")
        reused_id = _message("same-id", 11.0, row_id=9, text="different authoritative row")
        other_stream = _message("foreign", 12.0, chat_id="other-chat", row_id=10)
        future_unrelated = _message("future", 99.0, row_id=11)

        merged = merge_planner_context_messages(
            [history_copy, reused_id, other_stream, future_unrelated],
            [snapshot_copy],
            chat_id="context-chat",
            limit=10,
            history_before=20.0,
        )

        self.assertEqual([message.focus_row_id for message in merged], [8, 9])
        self.assertEqual(merged[0].processed_plain_text, "snapshot with file preview")
        self.assertNotIn("other-chat", [message.chat_id for message in merged])

    async def _exercise_planner(self, planner_type, module, *, blocked_user_ids=None):
        now = time.time()
        history = _message("history-id", now - 5, row_id=1, text="Earlier: prior context")
        wake = _message("wake-id", now + 1, row_id=2, nickname="Alice", text="Please inspect report.csv [file preview: quarterly totals]")
        direct = _message(
            "direct-id",
            now + 2,
            row_id=3,
            nickname="Direct",
            text="direct reply context",
            planner_bypass=True,
        )
        blocked = _message("blocked-id", now + 3, row_id=4, user_id="blocked-user", text="blocked context")
        held = _message("held-id", now + 4, row_id=5, text="held recovery context", held_recovery=True)
        unrelated_future = _message("future-id", now + 20, row_id=6, text="unrelated future")
        other_stream = _message("other-stream-id", now - 1, chat_id="other-chat", row_id=7, text="other stream")

        rendered = {}

        def render_messages(messages, **_kwargs):
            rows = list(messages)
            rendered["rows"] = rows
            rendered["prompt"] = "\n".join(
                f"{message.user_info.user_nickname}: {message.processed_plain_text}"
                for message in rows
                if message.user_info is not None
            )
            return rendered["prompt"], [(message.message_id, message) for message in rows]

        async def build_prompt(**kwargs):
            return kwargs["chat_content_block"], kwargs["message_id_list"]

        planner = object.__new__(planner_type)
        planner.chat_id = "context-chat"
        planner.log_prefix = "[context-chat]"
        planner.last_obs_time_mark = 0.0
        planner.tool_executor = Mock()
        planner.get_necessary_info = Mock(return_value=(planner_type is BrainPlanner, None, {}))
        planner._filter_actions_by_activation_type = Mock(return_value={})
        planner.build_planner_prompt = AsyncMock(side_effect=build_prompt)
        execute = AsyncMock(return_value=[])
        planner._execute_main_planner = execute

        with patch("src.chat.offline_recovery.offline_recovery_manager", _RecoveryFilter()):
            with patch.object(module.global_config.chat, "get_max_context_size", return_value=120), \
                patch.object(chat_message_builder_module, "count_messages", return_value=1), \
                patch.object(
                    module,
                    "get_raw_msg_before_timestamp_with_chat",
                    return_value=[history, unrelated_future, other_stream],
                ) as history_query, \
                patch.object(module, "build_readable_messages_with_id", side_effect=render_messages):
                if planner_type is ActionPlanner:
                    with patch.object(module, "can_offer_switch_chat", return_value=False):
                        await planner.plan(
                            available_actions={},
                            blocked_user_ids=blocked_user_ids,
                            trigger_messages=[wake, direct, blocked, held],
                        )
                else:
                    with patch.object(module, "has_active_focus_lease", return_value=False), \
                        patch.object(module.global_config.focus, "mode", "off"), \
                        patch.object(module.global_config.bot, "integrated_plan", False):
                        await planner.plan(
                            available_actions={},
                            trigger_messages=[wake, direct, held],
                        )

        prompt = execute.await_args.kwargs["prompt"]
        self.assertIn("Alice: Please inspect report.csv [file preview: quarterly totals]", prompt)
        self.assertEqual(prompt.count("quarterly totals"), 1)
        self.assertNotIn("unrelated future", prompt)
        self.assertNotIn("other stream", prompt)
        self.assertNotIn("held recovery context", prompt)
        self.assertIn("direct reply context", prompt)
        message_ids = [message_id for message_id, _message in execute.await_args.kwargs["message_id_list"]]
        self.assertIn("wake-id", message_ids)
        self.assertNotIn("direct-id", message_ids)
        self.assertEqual(len(rendered["rows"]), 3)
        self.assertEqual(history_query.call_args.kwargs["limit"], 1)
        return prompt

    async def test_brain_and_action_planners_execute_with_explicit_wake_context(self):
        await self._exercise_planner(BrainPlanner, brain_planner_module)
        await self._exercise_planner(
            ActionPlanner,
            action_planner_module,
            blocked_user_ids={"blocked-user"},
        )

    async def _exercise_observe_caller(self, runtime_type, module):
        class _PlannerReached(RuntimeError):
            pass

        @asynccontextmanager
        async def prompt_scope(_template):
            yield

        now = time.time()
        history = _message("observe-history", now - 5, row_id=40, text="observed earlier history")
        wake_messages = [
            _message("observe-wake-1", now + 1, row_id=41, text="first admitted wake"),
            _message("observe-wake-2", now + 2, row_id=42, text="second admitted wake"),
        ]
        stream = SimpleNamespace(
            stream_id="context-chat",
            platform="test",
            group_info=SimpleNamespace(group_id="group-1"),
            context=SimpleNamespace(get_template_name=Mock(return_value=None)),
        )
        runtime = object.__new__(runtime_type)
        runtime.stream_id = "context-chat"
        runtime.log_prefix = "[context-chat]"
        runtime.chat_stream = stream
        runtime._cycle_counter = 0
        runtime.expression_learner = SimpleNamespace(trigger_learning_for_chat=AsyncMock())
        runtime.action_modifier = SimpleNamespace(modify_actions=AsyncMock())
        runtime.action_manager = SimpleNamespace(get_using_actions=Mock(return_value={}))
        runtime.last_read_time = 0.0
        runtime._planner_interrupt_flag = None
        runtime._planner_interrupt_requested = False
        runtime._planner_interrupt_consecutive_count = 0
        runtime._focus_turn_interrupted = False
        runtime._focus_delivered_event_revisions = None
        runtime.start_cycle = Mock(return_value=({}, "thinking"))
        runtime.end_cycle = Mock()
        runtime.print_cycle_info = Mock()
        if runtime_type is HeartFChatting:
            runtime.blocked_users = {}
            runtime._filter_blocked_users = Mock(side_effect=lambda rows, **_kwargs: list(rows))

        planner = SimpleNamespace(
            get_necessary_info=Mock(return_value=(True, None, {})),
            last_obs_time_mark=0.0,
            build_planner_prompt=AsyncMock(return_value=("prebuilt prompt", [])),
            plan=AsyncMock(side_effect=_PlannerReached("planner call reached")),
        )
        runtime.action_planner = planner
        rendered_rows = []

        def render_messages(messages, **_kwargs):
            rows = list(messages)
            rendered_rows.extend(rows)
            return "\n".join(row.processed_plain_text or "" for row in rows), [
                (row.message_id, row) for row in rows
            ]

        class _ContextRecoveryFilter(_RecoveryFilter):
            @staticmethod
            def filter_recovery_rows_from_context(_chat_id, messages):
                return list(messages)

        history_query = Mock(return_value=[history])
        patches = [
            patch.object(module, "get_chat_manager", return_value=SimpleNamespace(get_stream=Mock(return_value=stream))),
            patch.object(module.global_prompt_manager, "async_message_scope", prompt_scope),
            patch.object(module.global_prompt_manager, "get_prompt_async", new=AsyncMock(return_value="debug prompt")),
            patch.object(module.global_config.chat, "get_max_context_size", return_value=120),
            patch.object(chat_message_builder_module, "count_messages", return_value=1),
            patch.object(module, "get_raw_msg_before_timestamp_with_chat", new=history_query),
            patch.object(module, "build_readable_messages_with_id", side_effect=render_messages),
            patch.object(module, "offline_recovery_manager", _ContextRecoveryFilter()),
            patch.object(
                module.events_manager,
                "handle_nacho_events",
                new=AsyncMock(return_value=(True, None)),
            ),
        ]
        if runtime_type is BrainChatting:
            patches.extend(
                [
                    patch.object(module.advanced_manager, "is_on", return_value=False),
                    patch(
                        "src.memory_system.person_profile_injector.inject_person_profiles",
                        new=AsyncMock(return_value="observed context"),
                    ),
                ]
            )
        else:
            patches.extend(
                [
                    patch.object(module, "runtime_capabilities_from_stream", return_value=SimpleNamespace(notice_actions=False)),
                    patch(
                        "src.memory_system.heuristic_memory_injector.inject_memory_context",
                        new=AsyncMock(return_value="observed context"),
                    ),
                    patch(
                        "src.memory_system.person_profile_injector.inject_person_profiles",
                        new=AsyncMock(return_value="observed context"),
                    ),
                ]
            )

        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with self.assertRaises(_PlannerReached):
                await runtime._observe(recent_messages_list=wake_messages)

        self.assertEqual(history_query.call_args.kwargs["limit"], 1)
        self.assertEqual(
            [row.message_id for row in rendered_rows],
            ["observe-history", "observe-wake-1", "observe-wake-2"],
        )
        self.assertEqual(planner.plan.await_args.kwargs["trigger_messages"], wake_messages)

    async def test_brain_and_heart_observers_keep_sparse_history_wake_batch(self):
        await self._exercise_observe_caller(BrainChatting, brain_chat_module)
        await self._exercise_observe_caller(HeartFChatting, heart_chat_module)

    async def _exercise_large_planner_batch(
        self,
        planner_type,
        module,
        *,
        context_size=120,
        history_count=0,
        expected_capacity=72,
    ):
        now = time.time()
        history = _message("tiny-history", now - 5, row_id=99, text="tiny prior history")
        trigger_messages = [
            _message(
                f"batch-{index}",
                now + index / 100,
                row_id=100 + index,
                nickname="Batch Sender",
                text=f"explicit batch row {index}",
            )
            for index in range(100)
        ]
        rendered_rows = []

        def render_messages(messages, **_kwargs):
            rows = list(messages)
            rendered_rows.append(rows)
            return "\n".join(row.processed_plain_text for row in rows), [
                (row.message_id, row) for row in rows
            ]

        async def build_prompt(**kwargs):
            return kwargs["chat_content_block"], kwargs["message_id_list"]

        planner = object.__new__(planner_type)
        planner.chat_id = "context-chat"
        planner.log_prefix = "[context-chat]"
        planner.last_obs_time_mark = 0.0
        planner.tool_executor = Mock()
        planner.get_necessary_info = Mock(return_value=(planner_type is BrainPlanner, None, {}))
        planner._filter_actions_by_activation_type = Mock(return_value={})
        planner.build_planner_prompt = AsyncMock(side_effect=build_prompt)
        execute = AsyncMock(return_value=[])
        planner._execute_main_planner = execute
        history_query = Mock(return_value=[history] if history_count else [])

        with (
            patch("src.chat.offline_recovery.offline_recovery_manager", _RecoveryFilter()),
            patch.object(module.global_config.chat, "get_max_context_size", return_value=context_size),
            patch.object(chat_message_builder_module, "count_messages", return_value=history_count),
            patch.object(module, "get_raw_msg_before_timestamp_with_chat", new=history_query),
            patch.object(module, "build_readable_messages_with_id", side_effect=render_messages),
        ):
            if planner_type is ActionPlanner:
                with patch.object(module, "can_offer_switch_chat", return_value=False):
                    await planner.plan(available_actions={}, trigger_messages=trigger_messages)
            else:
                with (
                    patch.object(module, "has_active_focus_lease", return_value=False),
                    patch.object(module.global_config.focus, "mode", "off"),
                    patch.object(module.global_config.bot, "integrated_plan", False),
                ):
                    await planner.plan(available_actions={}, trigger_messages=trigger_messages)

        expected_query_limit = 0 if history_count == 0 else expected_capacity
        self.assertEqual(history_query.call_args.kwargs["limit"], expected_query_limit)
        self.assertEqual(len(rendered_rows[0]), expected_capacity)
        self.assertEqual(rendered_rows[0][0].message_id, f"batch-{100 - expected_capacity}")
        self.assertEqual(rendered_rows[0][-1].message_id, "batch-99")
        self.assertEqual(len(execute.await_args.kwargs["message_id_list"]), expected_capacity)

    async def test_zero_history_count_keeps_batch_bounded_by_configured_capacity(self):
        await self._exercise_large_planner_batch(BrainPlanner, brain_planner_module)
        await self._exercise_large_planner_batch(ActionPlanner, action_planner_module)

    async def test_fractional_planner_windows_are_clamped_before_stepping(self):
        await self._exercise_large_planner_batch(
            BrainPlanner,
            brain_planner_module,
            context_size=1,
            history_count=1,
            expected_capacity=1,
        )
        await self._exercise_large_planner_batch(
            ActionPlanner,
            action_planner_module,
            context_size=1,
            history_count=1,
            expected_capacity=1,
        )

    async def test_integrated_brain_planner_short_context_includes_wake_snapshot(self):
        now = time.time()
        wake = _message("integrated-wake", now + 1, row_id=20, nickname="Alice", text="integrated file preview")
        history = _message("integrated-history", now - 5, row_id=19, text="previous chat")
        short_rows = []
        short_prompts = []

        def render_messages(messages, **_kwargs):
            rows = list(messages)
            short_rows.extend(rows)
            return "\n".join(message.processed_plain_text or "" for message in rows)

        async def build_relation(*_args, **_kwargs):
            return ""

        async def build_memory(*_args, **_kwargs):
            short_prompts.append(_kwargs.get("message", ""))
            return ""

        async def build_knowledge(*_args, **_kwargs):
            return ""

        planner = object.__new__(BrainPlanner)
        planner.chat_id = "context-chat"
        planner.log_prefix = "[context-chat]"
        planner.tool_executor = Mock()
        planner._build_action_options_block = AsyncMock(return_value="")
        manager = SimpleNamespace(get_stream=Mock(return_value=SimpleNamespace(stream_id="context-chat")))

        with (
            patch("src.chat.offline_recovery.offline_recovery_manager", _RecoveryFilter()),
            patch.object(brain_planner_module, "get_actions_by_timestamp_with_chat", return_value=[]),
            patch.object(chat_message_builder_module, "count_messages", return_value=1),
            patch.object(
                brain_planner_module,
                "get_raw_msg_before_timestamp_with_chat",
                return_value=[history],
            ) as history_query,
            patch.object(brain_planner_module, "build_readable_messages", side_effect=render_messages),
            patch.object(brain_planner_module, "build_relation_info", side_effect=build_relation),
            patch.object(brain_planner_module, "build_memory_retrieval_prompt", side_effect=build_memory),
            patch.object(brain_planner_module, "build_lpmm_knowledge_info", side_effect=build_knowledge),
            patch.object(brain_planner_module, "get_chat_manager", return_value=manager),
            patch.object(brain_planner_module.global_config.chat, "get_max_context_size", return_value=10),
            patch.object(
                brain_planner_module.global_config.expression,
                "get_expression_config_for_chat",
                return_value=(False, None, None),
            ),
            patch.object(brain_planner_module.global_config.bot, "nickname", "Nacho"),
            patch.object(brain_planner_module.global_config.bot, "alias_names", []),
            patch.object(brain_planner_module.global_config.personality, "personality", "helpful"),
            patch.object(brain_planner_module.global_config.personality, "private_plan_style", "normal"),
            patch.object(
                brain_planner_module.global_prompt_manager,
                "get_prompt_async",
                new=AsyncMock(return_value="{dialogue_prompt}"),
            ),
            patch.object(brain_planner_module, "render_dynamic_prompt_template", return_value="helpful"),
        ):
            prompt, _ids = await planner.build_integrated_planner_prompt(
                is_group_chat=False,
                chat_target_info=None,
                current_available_actions={},
                message_id_list=[("integrated-wake", wake)],
                chat_content_block="primary prompt has wake",
                trigger_messages=[wake],
            )

        self.assertIn("primary prompt has wake", prompt)
        self.assertIn("integrated file preview", short_prompts[0])
        self.assertEqual(sum(row.message_id == "integrated-wake" for row in short_rows), 1)
        self.assertLessEqual(len(short_rows), 2)
        self.assertEqual(history_query.call_args.kwargs["limit"], 1)

    async def _exercise_replyer_context(self, replyer_type, module, *, held_target=False):
        now = time.time()
        target = _message(
            "reply-target",
            now + 1,
            row_id=30,
            nickname="Alice",
            text="future reply target [file preview: important attachment]",
            held_recovery=held_target,
        )
        history = _message("reply-history", now - 5, row_id=29, text="prior reply history")
        held_history = _message(
            "held-history",
            now - 4,
            row_id=28,
            text="held recovery history",
            held_recovery=True,
        )
        unrelated_future = _message("unrelated-future", now + 20, row_id=31, text="unrelated future")
        stream = SimpleNamespace(
            stream_id="context-chat",
            platform="test",
            group_info=SimpleNamespace(group_id="group-1") if replyer_type is DefaultReplyer else None,
            user_info=SimpleNamespace(user_id="context-user", user_nickname="Context User", platform="test"),
        )
        replyer = object.__new__(replyer_type)
        replyer.chat_stream = stream
        replyer.request_type = "replyer"
        for name, result in (
            ("build_expression_habits", ("", [])),
            ("build_relation_info", ""),
            ("build_tool_info", ""),
            ("get_prompt_info", ""),
            ("build_actions_prompt", ""),
            ("build_personality_prompt", ""),
            ("_build_mid_term_memory_block", ""),
            ("build_keywords_reaction_prompt", ""),
        ):
            setattr(replyer, name, AsyncMock(return_value=result))

        visible_rows = []
        memory_contexts = []
        split_long_rows = []

        def render_readable(messages, **_kwargs):
            rows = list(messages)
            visible_rows.append(rows)
            return "\n".join(row.processed_plain_text or "" for row in rows)

        def split_history(messages, *_args):
            split_long_rows.extend(messages)
            return "core history", "background history"

        async def build_memory(*_args, **kwargs):
            memory_contexts.append(kwargs.get("message", ""))
            return ""

        pending_filter = _RecoveryFilter()
        history_query = Mock(return_value=[history, held_history, unrelated_future])
        format_prompt = AsyncMock(return_value="formatted reply prompt")
        patches = [
            patch.object(module.global_config.chat, "get_max_context_size", return_value=72),
            patch.object(module.global_config.mood, "enable_mood", False),
            patch.object(module.global_config.bot, "qq_account", "bot-user"),
            patch.object(module.global_config.bot, "platform", "test"),
            patch.object(module, "get_latest_session_name", return_value="session"),
            patch.object(chat_message_builder_module, "count_messages", return_value=1),
            patch.object(module, "get_raw_msg_before_timestamp_with_chat", new=history_query),
            patch.object(module, "build_readable_messages", side_effect=render_readable),
            patch.object(module, "build_memory_retrieval_prompt", side_effect=build_memory),
            patch.object(module, "replace_user_references", side_effect=lambda text, *_args, **_kwargs: text),
            patch.object(module, "guard_user_content", side_effect=lambda text, *_args: (text, False, [])),
            patch.object(module, "Person", side_effect=lambda **_kwargs: SimpleNamespace(is_known=False, person_name="Alice")),
            patch.object(module.global_prompt_manager, "format_prompt", new=format_prompt),
            patch("src.chat.offline_recovery.offline_recovery_manager", pending_filter),
        ]
        if replyer_type is DefaultReplyer:
            patches.append(patch.object(replyer, "build_split_chat_history_prompts", side_effect=split_history))
        else:
            patches.extend(
                [
                    patch.object(module.advanced_manager, "is_on", return_value=False),
                    patch.object(module.global_config.advanced, "block_tools_when_on", False),
                    patch.object(module.global_config.advanced, "block_tts_when_on", False),
                    patch.object(module.global_config.advanced, "prompt", ""),
                ]
            )

        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            await replyer.build_prompt_reply_context(reply_message=target)

        long_rows = split_long_rows if replyer_type is DefaultReplyer else visible_rows[0]
        short_rows = visible_rows[-1]
        target_context_expected = not held_target
        for rows in (long_rows, short_rows):
            self.assertEqual(sum(row.message_id == "reply-target" for row in rows), int(target_context_expected))
            self.assertIn("reply-history", [row.message_id for row in rows])
            self.assertNotIn("held-history", [row.message_id for row in rows])
            self.assertNotIn("unrelated-future", [row.message_id for row in rows])
        self.assertEqual(memory_contexts.count("prior reply history\nfuture reply target [file preview: important attachment]"), int(target_context_expected))
        self.assertEqual(memory_contexts.count("prior reply history"), int(not target_context_expected))
        self.assertEqual(
            [call.kwargs["limit"] for call in history_query.call_args_list],
            [1, 1],
        )

    async def test_group_and_private_replyers_merge_future_target_after_recovery_filter(self):
        for replyer_type, module in (
            (DefaultReplyer, group_replyer_module),
            (PrivateReplyer, private_replyer_module),
        ):
            with self.subTest(replyer=replyer_type.__name__, held=False):
                await self._exercise_replyer_context(replyer_type, module)
            with self.subTest(replyer=replyer_type.__name__, held=True):
                await self._exercise_replyer_context(replyer_type, module, held_target=True)

    def test_tts_action_type_gate_uses_advertised_message_formats(self):
        # Mirrors TTSAction's Core contract without importing the live plugin.
        tts_action_info = ActionInfo(
            name="tts_action",
            component_type=ComponentType.ACTION,
            associated_types=["tts_text"],
        )
        modifier = object.__new__(ActionModifier)
        modifier.log_prefix = "[test]"

        def context_for(accept_format):
            message = SimpleNamespace(
                message_info=SimpleNamespace(
                    format_info=SimpleNamespace(accept_format=accept_format),
                )
            )
            return ChatMessageContext(message)

        advertised = context_for(["text", "tts_text"])
        absent = context_for(["text"])
        self.assertTrue(advertised.check_types(["tts_text"]))
        self.assertEqual(modifier._check_action_associated_types({"tts_action": tts_action_info}, advertised), [])
        self.assertFalse(absent.check_types(["tts_text"]))
        self.assertEqual(
            [name for name, _reason in modifier._check_action_associated_types({"tts_action": tts_action_info}, absent)],
            ["tts_action"],
        )


if __name__ == "__main__":
    unittest.main()
