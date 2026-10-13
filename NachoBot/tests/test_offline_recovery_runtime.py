import asyncio
import json
import tempfile
import time
import unittest
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from peewee import SqliteDatabase

from src.chat.planner_actions.planner import ActionPlanner  # noqa: F401  # Prime generator dependencies.
from src.chat.replyer.group_generator import DefaultReplyer  # noqa: F401
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.focus.message_repository import load_message_batch, load_message_rows_by_ids
from src.chat.focus.models import FocusRecoveryBatch, StoredMessageRef
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.chat.heart_flow.heartflow_message_processor import HeartFCMessageReceiver
from src.chat.offline_recovery import (
    OfflineRecoveryManager,
    format_recovery_reply_context,
    is_offline_recovery_row,
)
from src.common.data_models.database_data_model import DatabaseMessages
from src.common.database.database_model import Messages
from src.common.data_models.info_data_model import ActionPlannerInfo
from src.plugin_system.apis.send_api import SendReceipt, SendStatus
import src.chat.brain_chat.brain_chat as brain_module
import src.chat.heart_flow.heartFC_chat as heart_module
import src.chat.heart_flow.heartflow_message_processor as processor_module


@asynccontextmanager
async def _prompt_scope(_template):
    yield


def _recovery_messages(chat_id: str, count: int = 27) -> list[DatabaseMessages]:
    messages = []
    for index in range(count):
        timestamp = 1_780_000_000.125 + index / 10
        message = DatabaseMessages(
            message_id=f"offline-{index + 1}",
            time=timestamp,
            chat_id=chat_id,
            processed_plain_text=f"offline-content-{index + 1}",
            display_message=f"offline-content-{index + 1}",
            additional_config=json.dumps(
                {
                    "offline_recovery": {
                        "version": 1,
                        "original_timestamp": timestamp,
                        "connection_epoch_started_at": timestamp + 100,
                        "progress_id": f"progress-{index + 1}",
                    }
                }
            ),
            user_id="sender-1",
            user_nickname="Sender",
            user_platform="qq",
            chat_info_stream_id=chat_id,
            chat_info_platform="qq",
        )
        message.focus_row_id = index + 1
        messages.append(message)
    return messages


def _runtime(runtime_type, module, chat_id, messages):
    stream = SimpleNamespace(
        stream_id=chat_id,
        platform="qq",
        group_info=None,
        context=SimpleNamespace(get_template_name=Mock(return_value=None)),
    )
    runtime = object.__new__(runtime_type)
    runtime.stream_id = chat_id
    runtime.log_prefix = f"[{chat_id}]"
    runtime.chat_stream = stream
    runtime._cycle_counter = 0
    runtime.expression_learner = SimpleNamespace(trigger_learning_for_chat=AsyncMock())
    runtime.action_modifier = SimpleNamespace(modify_actions=AsyncMock())
    runtime.action_manager = SimpleNamespace(get_using_actions=Mock(return_value={}))
    runtime.last_read_time = 1_780_000_200.0
    runtime.blocked_users = {}
    runtime._planner_interrupt_flag = None
    runtime._planner_interrupt_requested = False
    runtime._planner_interrupt_consecutive_count = 0
    runtime._focus_turn_interrupted = False
    runtime._focus_delivered_event_revisions = None
    runtime._offline_recovery_batch = FocusRecoveryBatch(
        batch_id=f"batch-{chat_id}",
        chat_id=chat_id,
        row_ids=tuple(message.focus_row_id for message in messages),
        min_original_timestamp=messages[0].time,
        stable_order=1,
    )
    runtime._offline_recovery_messages = messages
    runtime._offline_recovery_prompt = format_recovery_reply_context(messages)
    runtime._offline_recovery_delivered = False
    runtime._offline_recovery_dropped = False
    runtime.start_cycle = Mock(return_value=({}, "think-1"))
    runtime.end_cycle = Mock()
    runtime.print_cycle_info = Mock()
    runtime._focus_reply_context = Mock(return_value=None)
    runtime._send_and_store_reply = AsyncMock(return_value=({}, "delivered reply", {}))
    runtime._build_low_latency_person_profile_block = AsyncMock(return_value="")

    planner = SimpleNamespace(
        get_necessary_info=Mock(return_value=(False, None, {})),
        last_obs_time_mark=0.0,
        _filter_actions_by_activation_type=Mock(return_value={}),
    )
    runtime.action_planner = planner
    return runtime, planner, stream


class OfflineRecoveryRuntimeTests(unittest.TestCase):
    def test_recovery_row_loader_preserves_full_explicit_order_past_normal_limit(self):
        chat_id = "offline-recovery-explicit-order"
        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(str(Path(directory) / "messages.db"))
            with database.bind_ctx([Messages]):
                database.connect()
                database.create_tables([Messages])
                try:
                    stored_row_ids = []
                    for index in range(27):
                        row = Messages.create(
                            message_id=f"offline-row-{index + 1}",
                            time=1_780_000_000.0 + index,
                            chat_id=chat_id,
                            chat_info_stream_id=chat_id,
                            chat_info_platform="qq",
                            chat_info_user_platform="qq",
                            chat_info_user_id="42",
                            chat_info_user_nickname="Sender",
                            chat_info_create_time=1_780_000_000.0,
                            chat_info_last_active_time=1_780_000_000.0 + index,
                            user_platform="qq",
                            user_id="42",
                            user_nickname="Sender",
                            processed_plain_text=f"offline-content-{index + 1}",
                            is_command=False,
                        )
                        stored_row_ids.append(int(row.id))

                    explicit_row_ids = tuple(reversed(stored_row_ids))
                    with patch.object(brain_module.global_config.bot, "qq_account", 987654321):
                        recovered = load_message_rows_by_ids(chat_id, explicit_row_ids)
                        bounded = load_message_batch(
                            chat_id,
                            after_row_id=0,
                            through_row_id=max(stored_row_ids),
                            limit=20,
                        )

                    self.assertEqual(len(recovered), 27)
                    self.assertEqual(
                        tuple(message.focus_row_id for message in recovered),
                        explicit_row_ids,
                    )
                    self.assertEqual(len(bounded.messages), 20)
                finally:
                    database.drop_tables([Messages])
                    database.close()

    def test_live_rows_past_time_cursor_are_observed_by_both_runtimes_once(self):
        received_at = 1_780_000_000.0
        now = received_at + 3.0
        recent_time = now - 1.0
        database = SqliteDatabase(":memory:")
        manager = OfflineRecoveryManager()
        with database.bind_ctx([Messages]):
            database.connect()
            database.create_tables([Messages])
            try:
                for runtime_type, module, chat_id in (
                    (HeartFChatting, heart_module, "deferred-heart"),
                    (BrainChatting, brain_module, "deferred-brain"),
                ):
                    row = Messages.create(
                        message_id=f"slow-live-{chat_id}",
                        time=received_at,
                        chat_id=chat_id,
                        chat_info_stream_id=chat_id,
                        chat_info_platform="qq",
                        chat_info_user_platform="qq",
                        chat_info_user_id="42",
                        chat_info_user_nickname="Sender",
                        chat_info_create_time=received_at,
                        chat_info_last_active_time=received_at,
                        user_platform="qq",
                        user_id="42",
                        user_nickname="Sender",
                        processed_plain_text=f"slow live {chat_id}",
                        additional_config="{}",
                        is_command=False,
                    )
                    ingress = SimpleNamespace(
                        message_id=f"slow-live-{chat_id}",
                        message_info=SimpleNamespace(
                            user_info=SimpleNamespace(user_id="42"),
                            additional_config={},
                        ),
                    )
                    self.assertTrue(
                        manager.record_deferred_live_message(
                            ingress,
                            StoredMessageRef(int(row.id), chat_id, f"slow-live-{chat_id}", received_at),
                        )
                    )

                    current_message = DatabaseMessages(
                        message_id=f"recent-{chat_id}",
                        time=recent_time,
                        chat_id=chat_id,
                        processed_plain_text="newer normal row",
                        user_platform="qq",
                        user_id="43",
                        user_nickname="Other",
                        additional_config="{}",
                        is_mentioned=False,
                        is_at=False,
                        is_command=False,
                    )
                    runtime, _planner, _stream = _runtime(
                        runtime_type, module, chat_id, _recovery_messages(chat_id, 1)
                    )
                    runtime._offline_recovery_batch = None
                    runtime._offline_recovery_messages = []
                    runtime._offline_recovery_prompt = ""
                    runtime.last_read_time = now - 2.0
                    runtime._last_message_received_at = now - 1.0
                    runtime._observe = AsyncMock(return_value=True)
                    later_ingress = SimpleNamespace(
                        message_id=f"arrived-after-snapshot-{chat_id}",
                        message_info=SimpleNamespace(
                            user_info=SimpleNamespace(user_id="44"),
                            additional_config={},
                        ),
                    )
                    runtime._filter_blocked_users = Mock(side_effect=lambda messages, **_kwargs: list(messages))
                    if runtime_type is HeartFChatting:
                        runtime.blocked_users = {}
                        runtime.no_reply_until_call = False
                        runtime.talk_threshold = 1.0

                    time_query = Mock(side_effect=[[current_message], []])
                    cursor_commit = manager.commit_nonfocus_read_cursor
                    capture_state = {"late_ingress_captured": False}

                    def capture_after_observation_snapshot(
                        target_chat_id,
                        _cursor_commit=cursor_commit,
                        _chat_id=chat_id,
                        _later_ingress=later_ingress,
                        _capture_state=capture_state,
                    ):
                        _cursor_commit(target_chat_id)
                        if target_chat_id == _chat_id and not _capture_state["late_ingress_captured"]:
                            self.assertTrue(
                                manager.record_deferred_live_message(
                                    _later_ingress,
                                    StoredMessageRef(
                                        900,
                                        _chat_id,
                                        _later_ingress.message_id,
                                        now,
                                    ),
                                )
                            )
                            _capture_state["late_ingress_captured"] = True

                    with (
                        patch.object(module, "offline_recovery_manager", manager),
                        patch.object(
                            manager,
                            "commit_nonfocus_read_cursor",
                            side_effect=capture_after_observation_snapshot,
                        ),
                        patch.object(module.message_api, "get_messages_by_time_in_chat", time_query),
                        patch.object(
                            module,
                            "classify_runtime_message_batch",
                            return_value=SimpleNamespace(direct_messages=(current_message,)),
                        ),
                        patch.object(module.time, "time", return_value=now),
                        patch.object(module.asyncio, "sleep", new=AsyncMock()),
                    ):
                        result = asyncio.run(runtime._loopbody())
                        self.assertTrue(result)
                        observed = runtime._observe.await_args.kwargs["recent_messages_list"]
                        self.assertEqual(
                            [message.message_id for message in observed],
                            [f"slow-live-{chat_id}", f"recent-{chat_id}"],
                        )
                        self.assertEqual(time_query.call_args_list[0].kwargs["start_time"], now - 2.0)
                        self.assertGreater(time_query.call_args_list[0].kwargs["start_time"], received_at)
                        self.assertEqual(
                            manager._deferred_live_rows[chat_id],
                            {900: {f"message:{later_ingress.message_id}"}},
                        )

                        manager._deferred_live_rows.pop(chat_id)

                        awaitable = runtime._loopbody()
                        asyncio.run(awaitable)
                        self.assertEqual(runtime._observe.await_count, 1)
            finally:
                database.drop_tables([Messages])
                database.close()

    def test_actual_message_processor_snapshots_admitted_live_row_before_waking(self):
        chat_id = "processor-live-row"
        manager = OfflineRecoveryManager()
        runtime = SimpleNamespace(signal_new_message=Mock())
        now = 1_780_000_000.0
        message = SimpleNamespace(
            processed_plain_text="live message",
            is_picid=False,
            is_emoji=False,
            is_voice=False,
            is_mentioned=False,
            is_at=False,
            is_command=False,
            message_info=SimpleNamespace(
                platform="qq",
                message_id="processor-live-message",
                time=now,
                user_info=SimpleNamespace(
                    user_id="42",
                    user_nickname="Sender",
                    user_cardname=None,
                ),
                group_info=None,
                additional_config={},
            ),
            chat_stream=SimpleNamespace(stream_id=chat_id, group_info=None),
        )
        receiver = HeartFCMessageReceiver()
        receiver.storage = SimpleNamespace(
            store_message=AsyncMock(
                return_value=StoredMessageRef(7, chat_id, "processor-live-message", now)
            )
        )

        async def scenario():
            with (
                patch.object(processor_module, "offline_recovery_manager", manager),
                patch.object(processor_module, "_calculate_interest", new=AsyncMock(return_value=(1.0, []))),
                patch.object(
                    processor_module.focus_coordinator,
                    "route_message",
                    new=AsyncMock(return_value=SimpleNamespace(managed=False)),
                ),
                patch.object(processor_module.heartflow, "get_or_create_heartflow_chat", new=AsyncMock(return_value=runtime)),
                patch.object(processor_module.global_config.mood, "enable_mood", False),
                patch.object(processor_module, "replace_user_references", side_effect=lambda text, *_args, **_kwargs: text),
                patch.object(processor_module.Person, "register_person", return_value=None),
            ):
                await receiver.process_message(message)

        asyncio.run(scenario())

        self.assertEqual(manager._deferred_live_rows[chat_id], {7: {"message:processor-live-message"}})
        runtime.signal_new_message.assert_called_once_with(skip_interrupt=False)

    def test_deferred_live_load_failure_preserves_ids_for_runtime_retry(self):
        chat_id = "deferred-live-retry"
        manager = OfflineRecoveryManager()
        ingress = SimpleNamespace(
            message_id="deferred-retry-message",
            message_info=SimpleNamespace(
                user_info=SimpleNamespace(user_id="42"),
                additional_config={},
            ),
        )
        self.assertTrue(
            manager.record_deferred_live_message(
                ingress,
                StoredMessageRef(17, chat_id, ingress.message_id, 1_780_000_000.0),
            )
        )

        with (
            patch(
                "src.chat.focus.message_repository.load_message_rows_by_ids",
                side_effect=OSError("temporary SQLite read failure"),
            ),
            self.assertRaises(OSError),
        ):
            manager.include_deferred_live_messages(chat_id, [])

        self.assertEqual(manager._deferred_live_rows[chat_id], {17: {"message:deferred-retry-message"}})
        self.assertFalse(manager._deferred_live_observed.get(chat_id))

    def _exercise_recovery_turn(
        self,
        runtime_type,
        module,
        chat_id,
        *,
        messages=None,
        recovery=True,
        raw_history_rows=None,
    ):
        messages = list(messages) if messages is not None else _recovery_messages(chat_id)
        runtime, planner, stream = _runtime(runtime_type, module, chat_id, messages)
        if not recovery:
            runtime._offline_recovery_batch = None
            runtime._offline_recovery_messages = []
            runtime._offline_recovery_prompt = ""
        planner_messages = {}
        planner_rows = []
        reply_calls = []

        async def build_planner_prompt(**kwargs):
            planner_messages.update(kwargs)
            return kwargs["chat_content_block"], kwargs["message_id_list"]

        async def execute_planner(**kwargs):
            planner_rows.extend(kwargs["message_id_list"])
            # Point to an older prompt row: recovery normalization must still
            # create exactly one Replyer action for the newest eligible row.
            return [
                ActionPlannerInfo(
                    action_type="reply",
                    reasoning="planner result",
                    action_message=messages[0],
                    available_actions={},
                )
            ]

        async def run_planner(**_kwargs):
            return [
                ActionPlannerInfo(
                    action_type="reply",
                    reasoning="planner result",
                    action_message=messages[-1],
                    available_actions={},
                )
            ], None

        planner.build_planner_prompt = AsyncMock(side_effect=build_planner_prompt)
        planner._execute_main_planner = AsyncMock(side_effect=execute_planner)
        planner.plan = AsyncMock(side_effect=run_planner)

        async def generate_reply(**kwargs):
            reply_calls.append(kwargs)
            return True, SimpleNamespace(
                reply_set=object(),
                selected_expressions=None,
                context_refs=(),
                sandbox_edit_handoff=None,
            )

        caps = SimpleNamespace(planner_bypass=False, notice_actions=False, tool_mode="default", typo_enabled=True)
        with ExitStack() as stack:
            manager = stack.enter_context(patch.object(module, "get_chat_manager"))
            manager.return_value.get_stream.return_value = stream
            stack.enter_context(patch.object(module.global_prompt_manager, "async_message_scope", _prompt_scope))
            stack.enter_context(
                patch.object(module.global_prompt_manager, "get_prompt_async", new=AsyncMock(return_value="debug"))
            )
            stack.enter_context(patch.object(module.global_config.chat, "get_max_context_size", return_value=100))
            stack.enter_context(
                patch.object(
                    module,
                    "get_raw_msg_before_timestamp_with_chat",
                    return_value=(
                        list(raw_history_rows)
                        if raw_history_rows is not None
                        else ([] if recovery else list(messages))
                    ),
                )
            )
            stack.enter_context(patch.object(module.promise_cache_manager, "collect_snippets_for_messages", return_value=[]))
            stack.enter_context(patch.object(module.injection_manager, "build_injection_text", return_value=""))
            stack.enter_context(patch.object(module, "runtime_capabilities_from_message", return_value=caps))
            stack.enter_context(patch.object(module.generator_api, "generate_reply", new=AsyncMock(side_effect=generate_reply)))
            if runtime_type is HeartFChatting:
                stack.enter_context(patch.object(module, "runtime_capabilities_from_stream", return_value=caps))
                stack.enter_context(patch.object(module.global_prompt_manager, "get_prompt_async", new=AsyncMock(return_value="debug")))
                stack.enter_context(
                    patch.object(module.events_manager, "handle_nacho_events", new=AsyncMock(return_value=(True, None)))
                )
                stack.enter_context(patch.object(module, "get_stepped_limit", return_value=100))
                stack.enter_context(patch.object(module.focus_coordinator, "is_managed", return_value=False))
            else:
                stack.enter_context(patch.object(module.advanced_manager, "is_on", return_value=False))
                stack.enter_context(
                    patch.object(module.events_manager, "handle_nacho_events", new=AsyncMock(return_value=(True, None)))
                )
                stack.enter_context(
                    patch(
                        "src.memory_system.person_profile_injector.inject_person_profiles",
                        new=AsyncMock(side_effect=lambda **kwargs: kwargs["chat_content_block"]),
                    )
                )
                stack.enter_context(patch.object(module.focus_coordinator, "is_managed", return_value=False))
            result = asyncio.run(runtime._observe(recent_messages_list=list(messages)))

        self.assertTrue(result)
        planner.build_planner_prompt.assert_awaited_once()
        if recovery:
            planner._execute_main_planner.assert_awaited_once()
            prompt = planner._execute_main_planner.await_args.kwargs["prompt"]
        else:
            planner._execute_main_planner.assert_not_awaited()
            planner.plan.assert_awaited_once()
            prompt = planner_messages["chat_content_block"]
        for message in messages:
            self.assertIn(message.processed_plain_text, prompt)
            if recovery:
                self.assertIn(f'{message.time:.6f}', prompt)
        if recovery:
            self.assertGreaterEqual(len(planner_rows), len(messages))
            alias_by_row = {
                getattr(message, "focus_row_id", None): alias
                for alias, message in planner_rows
                if getattr(message, "focus_row_id", None) is not None
            }
            for message in messages:
                self.assertIn(f'planner_id="{alias_by_row[message.focus_row_id]}"', prompt)
        self.assertEqual(len(reply_calls), 1)
        reply_call = reply_calls[0]
        self.assertIs(reply_call["reply_message"], messages[-1])
        if recovery:
            for message in messages:
                self.assertIn(message.processed_plain_text, reply_call["extra_info"])
                local_time = datetime.fromtimestamp(message.time).astimezone()
                readable_time = local_time.strftime("%Y-%m-%d %H:%M:%S")
                if local_time.microsecond:
                    readable_time += f".{local_time.microsecond:06d}".rstrip("0")
                self.assertIn(readable_time, reply_call["extra_info"])
            self.assertNotIn("<offline_recovery_batch", reply_call["extra_info"])
            self.assertNotIn("</offline_recovery_batch>", reply_call["extra_info"])
            self.assertNotIn("<message index=", reply_call["extra_info"])
            self.assertNotIn("planner_id=", reply_call["extra_info"])
            self.assertNotIn("original_timestamp=", reply_call["extra_info"])
            self.assertNotIn("batch-", reply_call["extra_info"])
        runtime._send_and_store_reply.assert_awaited_once()

    def test_heartflow_recovery_passes_full_batch_to_planner_and_replyer_once(self):
        self._exercise_recovery_turn(HeartFChatting, heart_module, "offline-heartflow")

    def test_brain_recovery_passes_full_batch_to_planner_and_replyer_once(self):
        self._exercise_recovery_turn(BrainChatting, brain_module, "offline-brain")

    def test_old_notice_timestamp_stays_in_normal_planner_context(self):
        chat_id = "old-notice-normal-planner"
        received_at = 1_780_000_000.75
        notice = DatabaseMessages(
            message_id="notice-123",
            time=received_at,
            chat_id=chat_id,
            processed_plain_text="管理员开启了全体禁言",
            display_message="管理员开启了全体禁言",
            additional_config=json.dumps(
                {
                    "system_event": {
                        "version": 1,
                        "type": "qq.group_admin",
                        "actor": {"user_id": "42", "name": "管理员"},
                        "target": None,
                        "data": {"group_id": "77", "sub_type": "set"},
                    },
                    "notice_timestamp": {
                        "original_timestamp": received_at - 600.0,
                        "timestamp_source": "platform_seconds",
                    },
                },
                ensure_ascii=False,
            ),
            is_notify=True,
            chat_info_group_id="77",
            chat_info_group_name="群聊",
            chat_info_group_platform="qq",
            chat_info_stream_id=chat_id,
            chat_info_platform="qq",
        )
        notice.focus_row_id = 1
        self.assertFalse(is_offline_recovery_row(notice))

        self._exercise_recovery_turn(
            HeartFChatting,
            heart_module,
            chat_id,
            messages=[notice],
            recovery=False,
        )

    def test_both_runtimes_ack_only_a_delivered_receipt(self):
        async def exercise(runtime_type, chat_id, delivered):
            runtime = object.__new__(runtime_type)
            runtime.stream_id = chat_id
            runtime.chat_stream = SimpleNamespace(stream_id=chat_id)
            runtime.last_read_time = 1.0
            runtime._offline_recovery_batch = object()
            runtime._offline_recovery_delivered = False
            receipt = SendReceipt(
                SendStatus.DELIVERED if delivered else SendStatus.FAILED,
                chat_id,
            )

            async def send_permitted(**kwargs):
                receipts = kwargs["receipts"]
                receipts.append(receipt)
                return "reply" if delivered else "", receipts

            runtime._send_response_permitted = AsyncMock(side_effect=send_permitted)
            await runtime._send_response(reply_set=object(), message_data=object())
            self.assertEqual(runtime._offline_recovery_delivered, delivered)

        async def scenario():
            for runtime_type, chat_id in (
                (HeartFChatting, "receipt-heart"),
                (BrainChatting, "receipt-brain"),
            ):
                await exercise(runtime_type, chat_id, True)
                await exercise(runtime_type, f"{chat_id}-failed", False)

        asyncio.run(scenario())

    def test_nonfocus_recovery_false_result_retries_without_stopping_runtime(self):
        async def scenario():
            with (
                patch.object(brain_module.focus_coordinator, "is_managed", return_value=False),
                patch.object(brain_module.focus_coordinator, "is_recovery_held", return_value=False),
            ):
                for runtime_type, module, chat_id in (
                    (HeartFChatting, heart_module, "offline-heartflow-retry"),
                    (BrainChatting, brain_module, "offline-brain-retry"),
                ):
                    manager = OfflineRecoveryManager()
                    batch = FocusRecoveryBatch(
                        batch_id=f"batch-{chat_id}",
                        chat_id=chat_id,
                        row_ids=(1, 2),
                        min_original_timestamp=100.0,
                    )
                    manager._nonfocus_batches[chat_id] = [batch]
                    runtime = object.__new__(runtime_type)
                    runtime.stream_id = chat_id
                    runtime._offline_recovery_batch = None
                    runtime._offline_recovery_messages = []
                    runtime._offline_recovery_prompt = ""
                    runtime._offline_recovery_delivered = False
                    runtime._offline_recovery_dropped = False
                    runtime._in_flight_operations = 0
                    runtime._loopbody = AsyncMock(return_value=False)

                    with patch.object(module, "offline_recovery_manager", manager):
                        first_result = await runtime._run_tracked_loopbody()
                        self.assertTrue(first_result)
                        self.assertEqual(manager._nonfocus_batches[chat_id], [batch])
                        self.assertNotIn(chat_id, manager._nonfocus_claimed)
                        self.assertGreater(manager._nonfocus_retry_at[chat_id], time.monotonic())

                        retry_result = await runtime._run_tracked_loopbody()
                        self.assertTrue(retry_result)
                        runtime._loopbody.assert_awaited_once()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
