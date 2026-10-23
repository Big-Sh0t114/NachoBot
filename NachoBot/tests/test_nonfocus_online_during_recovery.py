from __future__ import annotations

import asyncio
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from peewee import SqliteDatabase

# Match the controlled integration setup and import order used by the Focus
# online-during-recovery coverage. It primes Replyer before either runtime.
import test_focus_online_during_recovery as focus_helpers
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.focus.coordinator import FocusCoordinator
from src.chat.focus.models import FocusRecoveryBatch, StoredMessageRef
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.chat.heart_flow.heartflow_message_processor import HeartFCMessageReceiver
from src.chat.message_receive.history_delivery import HistoryDeliveryStore
from src.chat.offline_recovery import OfflineRecoveryManager
from src.common.database.database_model import Messages
import src.chat.brain_chat.brain_chat as brain_module
import src.chat.heart_flow.heartFC_chat as heart_module
import src.chat.heart_flow.heartflow_message_processor as receiver_module
import src.chat.offline_recovery as recovery_module


_HELPERS = SimpleNamespace(
    _stream=focus_helpers.FocusOnlineDuringRecoveryTests._stream,
    _runtime=focus_helpers.FocusOnlineDuringRecoveryTests._runtime,
    _create_row=focus_helpers.FocusOnlineDuringRecoveryTests._create_row,
    _claim_pending=focus_helpers.FocusOnlineDuringRecoveryTests._claim_pending,
    _incoming_message=focus_helpers.FocusOnlineDuringRecoveryTests._incoming_message,
)


class _NoopApi:
    async def send_custom_message(self, *_args, **_kwargs):
        return None


class NonFocusOnlineDuringRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartflow_group_live_ingress_reaches_planner_during_open_history_run(self):
        await self._exercise_open_history_run(HeartFChatting, is_group=True)

    async def test_brain_private_live_ingress_reaches_planner_during_open_history_run(self):
        await self._exercise_open_history_run(BrainChatting, is_group=False)

    async def test_replay_only_ingress_does_not_wake_an_unmanaged_runtime(self):
        now = time.time()
        chat_id = "qq:group:replay-only"
        external_id = "replay-only"
        run_id = "open-replay-only-run"
        cutoff = now - 30.0
        history_time = now - 100.0
        coordinator = FocusCoordinator()
        stream = _HELPERS._stream(chat_id, group_id=external_id, group_name="Replay only")
        chat_manager = focus_helpers._ChatManager({chat_id: stream})
        manager = OfflineRecoveryManager(quiet_seconds=10.0, stale_progress_seconds=300.0)

        with TemporaryDirectory(prefix="nonfocus-replay-only-") as temp_dir:
            database = SqliteDatabase(":memory:")
            store = HistoryDeliveryStore(Path(temp_dir) / "history-delivery.sqlite3")
            with database.bind_ctx([Messages]):
                database.connect()
                database.create_tables([Messages])
                stack = ExitStack()
                try:
                    with stack:
                        self._patch_shared_ingress(stack, coordinator, manager, store, chat_manager)
                        stack.enter_context(patch("src.common.message.get_global_api", return_value=_NoopApi()))
                        wake = AsyncMock(return_value=None)
                        stack.enter_context(
                            patch.object(receiver_module.heartflow, "get_or_create_heartflow_chat", new=wake)
                        )
                        payload = self._history_run_payload(
                            run_id, external_id, "group", cutoff, source_instance="adapter-replay-only"
                        )
                        await manager.handle_history_run_message({"content": payload})

                        metadata = self._replay_metadata(
                            run_id, external_id, "group", cutoff, history_time, "replay-only-row"
                        )
                        row_id = _HELPERS._create_row(
                            chat_id,
                            "replay-only-row",
                            "RECOVERY_ONLY_NEVER_ORDINARY_INPUT",
                            history_time,
                            group_id=external_id,
                            additional_config=metadata,
                        )
                        recovery = metadata["offline_recovery"]
                        delivery = recovery["delivery"]
                        claim_status = store.claim(
                            {
                                **delivery,
                                "original_timestamp": history_time,
                                "history_run": recovery["history_run"],
                            },
                            now=now,
                        )
                        self.assertEqual(claim_status, "new")
                        receiver = object.__new__(HeartFCMessageReceiver)
                        receiver.storage = SimpleNamespace(
                            store_message=AsyncMock(
                                return_value=StoredMessageRef(row_id, chat_id, "replay-only-row", history_time)
                            )
                        )
                        await receiver.process_message(
                            _HELPERS._incoming_message(
                                stream,
                                external_id,
                                True,
                                "replay-only-row",
                                "RECOVERY_ONLY_NEVER_ORDINARY_INPUT",
                                metadata,
                            )
                        )

                        self.assertTrue(manager.is_held(chat_id))
                        self.assertEqual(wake.await_count, 0)
                        self.assertFalse(manager._deferred_live_rows.get(chat_id))
                        pending = store.pending_rows()
                        self.assertEqual([row["row_id"] for row in pending], [row_id])
                        self.assertEqual(pending[0]["original_timestamp"], history_time)
                finally:
                    await manager.shutdown()
                    store.close()
                    database.drop_tables([Messages])
                    database.close()

    async def test_unmanaged_wrappers_prioritize_live_work_and_preserve_claims(self):
        for runtime_type, is_group in ((HeartFChatting, True), (BrainChatting, False)):
            for backoff in (False, True):
                with self.subTest(runtime=runtime_type.__name__, backoff=backoff):
                    chat_id = f"qq:{'group' if is_group else 'private'}:claim-{runtime_type.__name__}-{backoff}"
                    external_id = chat_id.rsplit(":", 1)[-1]
                    coordinator = FocusCoordinator()
                    manager = OfflineRecoveryManager()
                    stream = (
                        _HELPERS._stream(chat_id, group_id=external_id, group_name="Claim test")
                        if is_group
                        else _HELPERS._stream(chat_id, user_id=external_id)
                    )
                    chat_manager = focus_helpers._ChatManager({chat_id: stream})
                    planner = focus_helpers._Planner(
                        is_group_chat=is_group,
                        gate_model=focus_helpers._GateModel(),
                        called=asyncio.Event(),
                    )
                    runtime = _HELPERS._runtime(runtime_type, chat_id, stream, planner)
                    live_message = _HELPERS._incoming_message(
                        stream,
                        external_id,
                        is_group,
                        "wrapper-live-row",
                        "wrapper live",
                        self._online_metadata(external_id, is_group, "wrapper-live-row", time.time() - 0.1),
                    )
                    self.assertTrue(
                        manager.record_deferred_live_message(
                            live_message,
                            StoredMessageRef(500, chat_id, "wrapper-live-row", time.time() - 0.1),
                        )
                    )

                    # Two additions coalesce into one chronological head. The
                    # live turn must leave that unclaimed head available.
                    manager._append_nonfocus_batches(
                        (
                            FocusRecoveryBatch("older-batch", chat_id, (41,), 10.0, 1, (10.0,)),
                            FocusRecoveryBatch("newer-batch", chat_id, (42,), 20.0, 2, (20.0,)),
                        )
                    )
                    queued_head = manager.peek_nonfocus_batch(chat_id)
                    self.assertIsNotNone(queued_head)
                    self.assertEqual(queued_head.row_ids, (41, 42))
                    if backoff:
                        manager.finish_nonfocus_batch(chat_id, queued_head.batch_id, delivered=False)
                        self.assertIsNone(manager.peek_nonfocus_batch(chat_id))

                    with ExitStack() as stack:
                        stack.enter_context(patch.object(recovery_module, "focus_coordinator", coordinator))
                        stack.enter_context(patch.object(recovery_module, "get_chat_manager", return_value=chat_manager))
                        module = heart_module if runtime_type is HeartFChatting else brain_module
                        stack.enter_context(patch.object(module, "focus_coordinator", coordinator))
                        stack.enter_context(patch.object(module, "offline_recovery_manager", manager))
                        stack.enter_context(patch.object(module, "get_chat_manager", return_value=chat_manager))
                        batches_seen_by_loopbody = []

                        async def ordinary_loopbody(
                            focus_turn=None,
                            *,
                            active_runtime=runtime,
                            observed_batches=batches_seen_by_loopbody,
                        ):
                            observed_batches.append(active_runtime._offline_recovery_batch)
                            return False

                        runtime._loopbody = AsyncMock(side_effect=ordinary_loopbody)
                        result = await runtime._run_tracked_loopbody()

                    self.assertTrue(result, "a failed ordinary turn must not stop a runtime that still has recovery work")
                    runtime._loopbody.assert_awaited_once_with(None)
                    self.assertEqual(batches_seen_by_loopbody, [None], "live work must run before claiming the recovery head")
                    self.assertNotIn(chat_id, manager._nonfocus_claimed)
                    queued_batches = manager._nonfocus_batches[chat_id]
                    self.assertEqual(len(queued_batches), 1)
                    self.assertEqual(queued_batches[0].row_ids, (41, 42))
                    if backoff:
                        manager._nonfocus_retry_at[chat_id] = 0.0
                    retained_head = manager.peek_nonfocus_batch(chat_id)
                    self.assertEqual(retained_head.row_ids, (41, 42))
                    self.assertTrue(manager.claim_nonfocus_batch(chat_id, retained_head.batch_id))
                    self.assertFalse(manager.claim_nonfocus_batch(chat_id, retained_head.batch_id))
                    self.assertTrue(manager.finish_nonfocus_batch(chat_id, retained_head.batch_id, delivered=False) is False)
                    self.assertEqual(manager.peek_nonfocus_batch(chat_id), None)
                    await manager.shutdown()

    async def _exercise_open_history_run(self, runtime_type, *, is_group: bool) -> None:
        now = time.time()
        external_id = "hfc-group" if is_group else "brain-peer"
        chat_id = f"qq:{'group' if is_group else 'private'}:{external_id}"
        run_id = f"open-{runtime_type.__name__}-history-run"
        cutoff = now - 30.0
        historical_time = now - 300.0
        live_time = now - 0.5
        history_text = "PENDING_HISTORY_MUST_STAY_OUT_OF_ORDINARY_PLANNER"
        live_text = "SAFE_GENUINE_ONLINE_MESSAGE"

        coordinator = FocusCoordinator()
        stream = (
            _HELPERS._stream(chat_id, group_id=external_id, group_name="Non-Focus group")
            if is_group
            else _HELPERS._stream(chat_id, user_id=external_id)
        )
        chat_manager = focus_helpers._ChatManager({chat_id: stream})
        manager = OfflineRecoveryManager(quiet_seconds=10.0, stale_progress_seconds=300.0)
        gate_model = focus_helpers._GateModel()
        planner_called = asyncio.Event()
        planner = focus_helpers._Planner(
            is_group_chat=is_group,
            gate_model=gate_model,
            called=planner_called,
        )
        prompt_inputs = []
        original_build = planner.build_planner_prompt

        async def record_prompt(**kwargs):
            prompt_inputs.append(kwargs)
            return await original_build(**kwargs)

        planner.build_planner_prompt = record_prompt
        runtime = _HELPERS._runtime(runtime_type, chat_id, stream, planner)
        runtime.last_read_time = now + 5.0
        runtime_tasks: list[asyncio.Task] = []

        with TemporaryDirectory(prefix="nonfocus-online-") as temp_dir:
            database = SqliteDatabase(":memory:")
            store = HistoryDeliveryStore(Path(temp_dir) / "history-delivery.sqlite3")
            with database.bind_ctx([Messages]):
                database.connect()
                database.create_tables([Messages])
                try:
                    with ExitStack() as stack:
                        self._patch_shared_ingress(stack, coordinator, manager, store, chat_manager)
                        stack.enter_context(patch("src.common.message.get_global_api", return_value=_NoopApi()))
                        self._patch_runtime_dependencies(stack)

                        async def start_controlled_runtime(requested_chat_id):
                            self.assertEqual(requested_chat_id, chat_id)
                            if not runtime.running:
                                runtime.running = True
                                task = asyncio.create_task(runtime._main_chat_loop(), name=f"isolated-{runtime_type.__name__}")
                                runtime_tasks.append(task)
                            return runtime

                        stack.enter_context(
                            patch.object(
                                receiver_module.heartflow,
                                "get_or_create_heartflow_chat",
                                new=AsyncMock(side_effect=start_controlled_runtime),
                            )
                        )

                        run_payload = self._history_run_payload(
                            run_id,
                            external_id,
                            "group" if is_group else "private",
                            cutoff,
                            source_instance=f"adapter-{runtime_type.__name__}",
                        )
                        await manager.handle_history_run_message({"content": run_payload})
                        self.assertTrue(manager.is_held(chat_id), "the open history marker must continue holding recovery")

                        history_row_id = _HELPERS._create_row(
                            chat_id,
                            "pending-history-row",
                            history_text,
                            historical_time,
                            group_id=external_id if is_group else None,
                            user_id="sender" if is_group else external_id,
                            additional_config={"offline_recovery": {"version": 1}},
                        )
                        _HELPERS._claim_pending(
                            store,
                            run_id,
                            "pending-history-delivery",
                            "pending-history-row",
                            "group" if is_group else "private",
                            external_id,
                            history_row_id,
                            historical_time,
                            now,
                        )
                        self.assertEqual(manager.restore_pending_deliveries(), 1)
                        self.assertEqual(manager.excluded_row_ids_for_chat(chat_id), {history_row_id})

                        live_metadata = self._online_metadata(external_id, is_group, "online-row", live_time)
                        live_row_id = _HELPERS._create_row(
                            chat_id,
                            "online-row",
                            live_text,
                            live_time,
                            group_id=external_id if is_group else None,
                            user_id="sender" if is_group else external_id,
                            additional_config=live_metadata,
                        )
                        receiver = object.__new__(HeartFCMessageReceiver)
                        receiver.storage = SimpleNamespace(
                            store_message=AsyncMock(
                                return_value=StoredMessageRef(live_row_id, chat_id, "online-row", live_time)
                            )
                        )
                        await receiver.process_message(
                            _HELPERS._incoming_message(
                                stream,
                                external_id,
                                is_group,
                                "online-row",
                                live_text,
                                live_metadata,
                            )
                        )

                        await asyncio.wait_for(planner_called.wait(), timeout=3.0)
                        self.assertEqual(len(planner.plan_calls), 1)
                        triggers = planner.plan_calls[0]["trigger_messages"]
                        self.assertEqual([row.processed_plain_text for row in triggers], [live_text])
                        planner_inputs = repr((planner.plan_calls, prompt_inputs))
                        self.assertNotIn(history_text, planner_inputs)
                        self.assertTrue(manager.is_held(chat_id))
                        self.assertNotIn(live_row_id, manager._deferred_live_rows.get(chat_id, {}))
                        self.assertEqual(len(store.pending_rows()), 1)
                        self.assertEqual(store.pending_rows()[0]["row_id"], history_row_id)
                        self.assertEqual(store.pending_rows()[0]["original_timestamp"], historical_time)
                        self.assertEqual(Messages.get_by_id(history_row_id).time, historical_time)
                        self.assertEqual(Messages.get_by_id(live_row_id).time, live_time)

                        # A second normal-loop pass after commit must not send
                        # the same deferred live row to Planner again.
                        await asyncio.sleep(0.25)
                        self.assertEqual(len(planner.plan_calls), 1)
                        self.assertTrue(manager.is_held(chat_id))
                finally:
                    runtime.running = False
                    for task in runtime_tasks:
                        if not task.done():
                            task.cancel()
                    if runtime_tasks:
                        await asyncio.gather(*runtime_tasks, return_exceptions=True)
                    await manager.shutdown()
                    store.close()
                    database.drop_tables([Messages])
                    database.close()

    @staticmethod
    def _patch_shared_ingress(stack, coordinator, manager, store, chat_manager):
        stack.enter_context(patch.object(recovery_module, "focus_coordinator", coordinator))
        stack.enter_context(patch.object(recovery_module, "history_delivery_store", store))
        stack.enter_context(patch.object(recovery_module, "get_chat_manager", return_value=chat_manager))
        stack.enter_context(patch.object(receiver_module, "focus_coordinator", coordinator))
        stack.enter_context(patch.object(receiver_module, "offline_recovery_manager", manager))
        stack.enter_context(patch.object(receiver_module, "_calculate_interest", new=AsyncMock(return_value=(1.0, []))))
        stack.enter_context(patch.object(receiver_module.Person, "register_person", return_value=None))
        stack.enter_context(
            patch.object(receiver_module, "replace_user_references", side_effect=lambda value, *_a, **_kw: value)
        )

    @staticmethod
    def _patch_runtime_dependencies(stack):
        for module in (heart_module, brain_module):
            stack.enter_context(patch.object(module, "focus_coordinator", FocusCoordinator()))
            stack.enter_context(patch.object(module, "get_stepped_limit", return_value=20))
            stack.enter_context(patch.object(module.global_prompt_manager, "async_message_scope", new=focus_helpers._prompt_scope))
            stack.enter_context(patch.object(module.global_config.chat, "get_max_context_size", return_value=100))
            stack.enter_context(patch.object(module.events_manager, "handle_nacho_events", new=AsyncMock(return_value=(True, None))))
        stack.enter_context(patch.object(heart_module, "focus_coordinator", recovery_module.focus_coordinator))
        stack.enter_context(patch.object(brain_module, "focus_coordinator", recovery_module.focus_coordinator))
        stack.enter_context(patch.object(heart_module, "offline_recovery_manager", receiver_module.offline_recovery_manager))
        stack.enter_context(patch.object(brain_module, "offline_recovery_manager", receiver_module.offline_recovery_manager))
        stack.enter_context(patch.object(heart_module, "get_chat_manager", return_value=recovery_module.get_chat_manager()))
        stack.enter_context(patch.object(brain_module, "get_chat_manager", return_value=recovery_module.get_chat_manager()))
        stack.enter_context(
            patch.object(
                heart_module,
                "runtime_capabilities_from_stream",
                return_value=SimpleNamespace(planner_bypass=False, notice_actions=False),
            )
        )
        stack.enter_context(patch.object(brain_module.advanced_manager, "is_on", return_value=False))
        stack.enter_context(patch.object(heart_module.random, "random", return_value=0.0))
        stack.enter_context(
            patch.object(
                heart_module.frequency_control_manager,
                "get_or_create_frequency_control",
                return_value=SimpleNamespace(get_talk_frequency_adjust=Mock(return_value=1.0)),
            )
        )
        stack.enter_context(patch("src.memory_system.heuristic_memory_injector.inject_memory_context", new=AsyncMock(side_effect=lambda **kw: kw["chat_content_block"])))
        stack.enter_context(patch("src.memory_system.person_profile_injector.inject_person_profiles", new=AsyncMock(side_effect=lambda **kw: kw["chat_content_block"])))
        stack.enter_context(patch.object(brain_module.global_prompt_manager, "get_prompt_async", new=AsyncMock(return_value="controlled brain prompt")))
        stack.enter_context(patch.object(receiver_module.global_config.mood, "enable_mood", False))

    @staticmethod
    def _history_run_payload(run_id, external_id, kind, cutoff, *, source_instance):
        return {
            "version": 1,
            "backend": "napcat",
            "account_id": "isolated-test-account",
            "platform": "qq",
            "source_instance": source_instance,
            "run_id": run_id,
            "run_token": f"token-{run_id}",
            "cutoff_at": cutoff,
            "phase": "begin",
            "targets": [{"session_kind": kind, "session_id": external_id}],
        }

    @staticmethod
    def _online_metadata(external_id, is_group, message_id, original_timestamp):
        received_at = time.time()
        return {
            "offline_recovery": {
                "version": 1,
                "connection_epoch_started_at": received_at - 30.0,
                "received_at": received_at,
                "original_timestamp": original_timestamp,
                "timestamp_source": "platform_seconds",
                "progress_id": f"online-{message_id}",
                "source_instance": "adapter-current",
                "delivery": {
                    "version": 1,
                    "backend": "napcat",
                    "account_id": "isolated-test-account",
                    "platform": "qq",
                    "session_kind": "group" if is_group else "private",
                    "session_id": external_id,
                    "message_id": message_id,
                    "delivery_key": f"live-{message_id}",
                    "recovered": False,
                },
            }
        }

    @classmethod
    def _replay_metadata(cls, run_id, external_id, kind, cutoff, original_timestamp, message_id):
        received_at = time.time()
        return {
            "offline_recovery": {
                "version": 1,
                "connection_epoch_started_at": received_at - 30.0,
                "received_at": received_at,
                "original_timestamp": original_timestamp,
                "timestamp_source": "platform_seconds",
                "progress_id": f"replay-{message_id}",
                "source_instance": "adapter-replay-only",
                "history_run": {"run_id": run_id, "cutoff_at": cutoff},
                "delivery": {
                    "version": 1,
                    "backend": "napcat",
                    "account_id": "isolated-test-account",
                    "platform": "qq",
                    "session_kind": kind,
                    "session_id": external_id,
                    "message_id": message_id,
                    "delivery_key": f"replay-{message_id}",
                    "recovered": True,
                },
            }
        }
