from __future__ import annotations

import asyncio
import json
import time
import unittest
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from peewee import SqliteDatabase

from src.chat.planner_actions.planner import ActionPlanner  # noqa: F401  # Prime Replyer imports.
from src.chat.replyer.group_generator import DefaultReplyer  # noqa: F401
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.focus.coordinator import FocusCoordinator
from src.chat.focus.models import ChatKind, FocusGroupDefinition, FocusMember, StoredMessageRef
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.chat.heart_flow.heartflow_message_processor import HeartFCMessageReceiver
from src.chat.message_receive.history_delivery import HistoryDeliveryStore
from src.chat.offline_recovery import OfflineRecoveryManager
from src.common.database.database_model import Messages
import src.chat.brain_chat.brain_chat as brain_module
import src.chat.heart_flow.heartFC_chat as heart_module
import src.chat.heart_flow.heartflow_message_processor as receiver_module
import src.chat.offline_recovery as recovery_module


@asynccontextmanager
async def _prompt_scope(_template):
    yield


class _ChatManager:
    def __init__(self, streams):
        self.streams = streams

    def get_stream_id(self, platform, external_id, *, is_group):
        return f"{platform}:{'group' if is_group else 'private'}:{external_id}"

    def get_stream(self, chat_id):
        return self.streams[chat_id]

    def get_stream_name(self, chat_id):
        return chat_id


class _GateModel:
    def __init__(self):
        self.prompts: list[str] = []
        self.called = asyncio.Event()

    async def generate_response_async(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        self.called.set()
        return ('{"decision":"stay","reason":"test stay"}', {})


class _Planner:
    def __init__(self, *, is_group_chat: bool, gate_model: _GateModel, called: asyncio.Event):
        self.planner_llm = gate_model
        self.last_obs_time_mark = 0.0
        self.is_group_chat = is_group_chat
        self.plan_called = called
        self.plan_calls: list[dict] = []

    def get_necessary_info(self):
        return self.is_group_chat, None, {}

    async def build_planner_prompt(self, **kwargs):
        return "controlled planner prompt", []

    async def plan(self, **kwargs):
        self.plan_calls.append(kwargs)
        self.plan_called.set()
        return [], "planner-complete"


class FocusOnlineDuringRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_ingress_runs_gate_and_private_planner_while_open_history_stays_pending(self):
        now = time.time()
        source_chat = "qq:group:source"
        background_group = "qq:group:background"
        private_chat = "qq:private:peer"
        group_id = "online-during-recovery"
        old_run_id = "open-old-run"
        current_run_id = "open-current-run"
        old_cutoff = now - 200.0
        current_cutoff = now - 80.0
        old_source_time = now - 5_000.0
        old_private_time = now - 3_000.0
        current_source_time = now - 1_000.0

        coordinator = FocusCoordinator(unread_event_threshold=1)
        coordinator.register_group(
            FocusGroupDefinition(
                group_id,
                (
                    FocusMember(source_chat, ChatKind.GROUP, display_name="Active group"),
                    FocusMember(background_group, ChatKind.GROUP, display_name="Background group"),
                    FocusMember(private_chat, ChatKind.PRIVATE, display_name="Private peer"),
                ),
                initial_chat_id=source_chat,
            ),
            cursors={source_chat: 0, background_group: 0, private_chat: 0},
            # Core bootstrap passes its committed cursor as the startup latest
            # boundary. The restored history rows below must not create a live wake.
            latest_rows={source_chat: 0, background_group: 0, private_chat: 0},
        )

        temporary_directory = self.enterContext(_TemporaryDirectory())
        database = SqliteDatabase(":memory:")
        store = HistoryDeliveryStore(Path(temporary_directory) / "history-delivery.sqlite3")
        manager = OfflineRecoveryManager(quiet_seconds=10.0, stale_progress_seconds=300.0)
        gate_model = _GateModel()
        hfc_plan_called = asyncio.Event()
        brain_plan_called = asyncio.Event()
        hfc_planner = _Planner(is_group_chat=True, gate_model=gate_model, called=hfc_plan_called)
        brain_planner = _Planner(is_group_chat=False, gate_model=gate_model, called=brain_plan_called)

        source_stream = self._stream(source_chat, group_id="source", group_name="Active group")
        background_stream = self._stream(background_group, group_id="background", group_name="Background group")
        private_stream = self._stream(private_chat, user_id="peer")
        chat_manager = _ChatManager(
            {source_chat: source_stream, background_group: background_stream, private_chat: private_stream}
        )

        source_runtime = self._runtime(HeartFChatting, source_chat, source_stream, hfc_planner)
        brain_runtime = self._runtime(BrainChatting, private_chat, private_stream, brain_planner)
        history_rows: dict[str, int] = {}
        stored_refs: dict[str, StoredMessageRef] = {}
        completed_turns: list[tuple[object, object]] = []
        turn_finished = {
            "source_event": asyncio.Event(),
            "source_local": asyncio.Event(),
            "private": asyncio.Event(),
        }
        loop_tasks: list[asyncio.Task] = []
        receipts: list[dict] = []

        class _NoopApi:
            async def send_custom_message(self, _platform, _name, payload):
                receipts.append(payload)

        def route_payload(run_id: str, source_instance: str, cutoff: float):
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
                "targets": [
                    {"session_kind": "group", "session_id": "source"},
                    {"session_kind": "private", "session_id": "peer"},
                ],
            }

        with database.bind_ctx([Messages]):
            database.connect()
            database.create_tables([Messages])
            try:
                old_payload = route_payload(old_run_id, "adapter-old", old_cutoff)
                current_payload = route_payload(current_run_id, "adapter-current", current_cutoff)
                store.begin_history_run(old_payload)
                store.begin_history_run(current_payload)

                history_rows["old-source"] = self._create_row(
                    source_chat, "old-source-row", "OLD_PENDING_HISTORY", old_source_time,
                    group_id="source", additional_config={"offline_recovery": {"version": 1}},
                )
                history_rows["current-source"] = self._create_row(
                    source_chat, "current-source-row", "CURRENT_PENDING_HISTORY", current_source_time,
                    group_id="source", additional_config={"offline_recovery": {"version": 1}},
                )
                history_rows["old-private"] = self._create_row(
                    private_chat, "old-private-row", "PRIVATE_PENDING_HISTORY", old_private_time,
                    user_id="peer", additional_config={"offline_recovery": {"version": 1}},
                )

                self._claim_pending(
                    store, old_run_id, "old-source-delivery", "old-source-row", "group", "source",
                    history_rows["old-source"], old_source_time, now,
                )
                self._claim_pending(
                    store, current_run_id, "current-source-delivery", "current-source-row", "group", "source",
                    history_rows["current-source"], current_source_time, now,
                )
                self._claim_pending(
                    store, old_run_id, "old-private-delivery", "old-private-row", "private", "peer",
                    history_rows["old-private"], old_private_time, now,
                )

                stack = ExitStack()
                with stack:
                    stack.enter_context(patch.object(recovery_module, "focus_coordinator", coordinator))
                    stack.enter_context(patch.object(recovery_module, "history_delivery_store", store))
                    stack.enter_context(patch.object(recovery_module, "get_chat_manager", return_value=chat_manager))
                    stack.enter_context(patch.object(receiver_module, "focus_coordinator", coordinator))
                    stack.enter_context(patch.object(receiver_module, "offline_recovery_manager", manager))
                    stack.enter_context(patch.object(receiver_module, "_calculate_interest", new=AsyncMock(return_value=(1.0, []))))
                    stack.enter_context(patch.object(receiver_module.Person, "register_person", return_value=None))
                    stack.enter_context(
                        patch.object(
                            receiver_module,
                            "replace_user_references",
                            side_effect=lambda value, *_args, **_kwargs: value,
                        )
                    )
                    stack.enter_context(patch.object(receiver_module.heartflow, "get_or_create_heartflow_chat", new=AsyncMock(return_value=source_runtime)))
                    stack.enter_context(patch("src.common.message.get_global_api", return_value=_NoopApi()))

                    for module in (heart_module, brain_module):
                        stack.enter_context(patch.object(module, "focus_coordinator", coordinator))
                        stack.enter_context(patch.object(module, "offline_recovery_manager", manager))
                        stack.enter_context(patch.object(module, "get_chat_manager", return_value=chat_manager))
                        stack.enter_context(patch.object(module, "get_stepped_limit", return_value=20))
                        stack.enter_context(patch.object(module.global_prompt_manager, "async_message_scope", new=_prompt_scope))
                        stack.enter_context(patch.object(module.global_config.chat, "get_max_context_size", return_value=100))
                        if module is heart_module:
                            stack.enter_context(
                                patch.object(
                                    module,
                                    "runtime_capabilities_from_stream",
                                    return_value=SimpleNamespace(planner_bypass=False, notice_actions=False),
                                )
                            )
                        else:
                            stack.enter_context(patch.object(module.advanced_manager, "is_on", return_value=False))
                        stack.enter_context(patch.object(module.events_manager, "handle_nacho_events", new=AsyncMock(return_value=(True, None))))
                        stack.enter_context(patch.object(module.promise_cache_manager, "collect_snippets_for_messages", return_value=[]))

                    stack.enter_context(patch.object(heart_module.random, "random", return_value=0.0))
                    stack.enter_context(patch("src.memory_system.heuristic_memory_injector.inject_memory_context", new=AsyncMock(side_effect=lambda **kwargs: kwargs["chat_content_block"])))
                    stack.enter_context(patch("src.memory_system.person_profile_injector.inject_person_profiles", new=AsyncMock(side_effect=lambda **kwargs: kwargs["chat_content_block"])))
                    stack.enter_context(patch.object(brain_module.global_prompt_manager, "get_prompt_async", new=AsyncMock(return_value="controlled brain prompt")))
                    stack.enter_context(patch.object(heart_module.global_config.focus, "mode", "active"))
                    stack.enter_context(patch.object(heart_module.global_config.focus, "allow_group_to_private", True, create=True))
                    stack.enter_context(patch.object(heart_module.global_config.focus, "bypass_gate_enabled", True))
                    stack.enter_context(patch.object(heart_module.global_config.chat, "planner_interrupt_enabled", False, create=True))
                    stack.enter_context(patch.object(receiver_module.global_config.mood, "enable_mood", False))

                    async def instrument_finish(turn, outcome):
                        result = await original_finish_turn(turn, outcome)
                        completed_turns.append((turn, outcome))
                        if turn.lease.chat_id == source_chat and turn.events:
                            turn_finished["source_event"].set()
                        elif turn.lease.chat_id == source_chat and turn.read_through_row_id >= history_rows.get("live-online-local-group", 10**9):
                            turn_finished["source_local"].set()
                        elif turn.lease.chat_id == private_chat and turn.read_through_row_id >= stored_refs.get("private-live", StoredMessageRef(1, private_chat, "sentinel", now)).row_id:
                            turn_finished["private"].set()
                        return result

                    original_finish_turn = coordinator.finish_turn
                    stack.enter_context(patch.object(coordinator, "finish_turn", side_effect=instrument_finish))
                    original_wait = coordinator.wait_for_turn
                    turns = []

                    async def instrument_wait(chat_id):
                        turn = await original_wait(chat_id)
                        turns.append(turn)
                        return turn

                    stack.enter_context(patch.object(coordinator, "wait_for_turn", side_effect=instrument_wait))

                    async def prepare_target(chat_id):
                        self.assertEqual(chat_id, private_chat)
                        brain_runtime.running = True
                        task = asyncio.create_task(brain_runtime._main_chat_loop(), name="isolated-private-focus-loop")
                        loop_tasks.append(task)

                    coordinator.set_ensure_runtime_callback(prepare_target)

                    async def route(row_id: int, chat_id: str, external_id: str, *, is_group: bool, message_id: str, text: str, metadata: dict):
                        stored_refs[message_id] = StoredMessageRef(row_id, chat_id, message_id, metadata["offline_recovery"]["original_timestamp"])
                        receiver = object.__new__(HeartFCMessageReceiver)
                        receiver.storage = SimpleNamespace(
                            store_message=AsyncMock(side_effect=lambda _message, _chat: stored_refs[message_id])
                        )
                        message = self._incoming_message(
                            chat_manager.get_stream(chat_id), external_id, is_group,
                            message_id, text, metadata,
                        )
                        await receiver.process_message(message)
                        return message

                    async def send_online(chat_id, external_id, *, is_group, message_id, text):
                        received_at = time.time()
                        metadata = {
                            "offline_recovery": {
                                "version": 1,
                                "connection_epoch_started_at": received_at - 30.0,
                                "received_at": received_at,
                                "original_timestamp": received_at - 0.2,
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
                        row_id = self._create_row(
                            chat_id, message_id, text, received_at - 0.2,
                            group_id=external_id if is_group else None,
                            user_id="sender" if is_group else external_id,
                            additional_config=metadata,
                        )
                        history_rows[f"live-{message_id}"] = row_id
                        await route(
                            row_id, chat_id, external_id, is_group=is_group,
                            message_id=message_id, text=text, metadata=metadata,
                        )

                    async def send_replay(row_id: int, *, message_id: str, original_timestamp: float):
                        delivery_key = f"replay-{message_id}"
                        metadata = {
                            "offline_recovery": {
                                "version": 1,
                                "connection_epoch_started_at": now - 20.0,
                                "received_at": now,
                                "original_timestamp": original_timestamp,
                                "timestamp_source": "platform_seconds",
                                "progress_id": f"replay-{message_id}",
                                "source_instance": "adapter-current",
                                "history_run": {"run_id": current_run_id, "cutoff_at": current_cutoff},
                                "delivery": {
                                    "version": 1,
                                    "backend": "napcat",
                                    "account_id": "isolated-test-account",
                                    "platform": "qq",
                                    "session_kind": "group",
                                    "session_id": "source",
                                    "message_id": message_id,
                                    "delivery_key": delivery_key,
                                    "recovered": True,
                                },
                            }
                        }
                        row_id = self._create_row(
                            source_chat, message_id, "NEW_PENDING_REPLAY", now - 20.0,
                            group_id="source", additional_config=metadata,
                        )
                        history_rows["replay-source"] = row_id
                        self._claim_pending(
                            store, current_run_id, delivery_key, message_id, "group", "source",
                            row_id, original_timestamp, now,
                        )
                        await route(
                            row_id, source_chat, "source", is_group=True,
                            message_id=message_id, text="NEW_PENDING_REPLAY", metadata=metadata,
                        )

                    await manager.handle_history_run_message({"content": old_payload})
                    await manager.handle_history_run_message({"content": current_payload})
                    manager.restore_pending_deliveries()
                    heartbeat = {
                        "version": 1,
                        "platform": "qq",
                        "source_instance": "adapter-current",
                        "progress_id": "healthy-current-scan",
                        "active": True,
                        "connection_epoch_started_at": now - 30.0,
                        "received_at": now,
                        "history_run": {"run_id": current_run_id, "cutoff_at": current_cutoff},
                        "group_id": "source",
                    }
                    await manager.handle_progress_message({"content": heartbeat})
                    self.assertTrue(coordinator._groups[group_id].recovery_collecting)
                    self.assertTrue(manager._active_progress_for(f"focus:{group_id}"))
                    self.assertEqual(
                        manager.excluded_row_ids_for_chat(source_chat),
                        {history_rows["old-source"], history_rows["current-source"]},
                    )

                    # A real managed HeartF runtime is already suspended in
                    # wait_for_turn while the recovered scan is healthy.
                    source_runtime.running = True
                    source_loop = asyncio.create_task(source_runtime._main_chat_loop(), name="isolated-group-focus-loop")
                    loop_tasks.append(source_loop)
                    await asyncio.sleep(0.02)

                    # Recovery-only ingress enters the same receiver/store path,
                    # but cannot wake ordinary Focus or invoke a model.
                    await send_replay(0, message_id="replay-live-during-scan", original_timestamp=now - 4_000.0)
                    await asyncio.sleep(0.02)
                    self.assertFalse(gate_model.called.is_set())
                    self.assertEqual(completed_turns, [])
                    self.assertTrue(coordinator._groups[group_id].recovery_collecting)

                    # A normal background GROUP message makes a visible event.
                    # Equal-priority GROUP routing reaches the real HFC Gate.
                    await send_online(
                        background_group, "background", is_group=True,
                        message_id="online-background-group", text="safe background group message",
                    )
                    await asyncio.wait_for(gate_model.called.wait(), timeout=2.0)
                    await asyncio.wait_for(turn_finished["source_event"].wait(), timeout=2.0)
                    self.assertIsNone(next(turn for turn in turns if turn.lease.chat_id == source_chat).recovery_batch)
                    self.assertNotIn("OLD_PENDING_HISTORY", gate_model.prompts[0])
                    self.assertNotIn("CURRENT_PENDING_HISTORY", gate_model.prompts[0])
                    self.assertNotIn("PRIVATE_PENDING_HISTORY", gate_model.prompts[0])
                    self.assertEqual(coordinator._groups[group_id].committed_cursor[source_chat], history_rows["replay-source"])

                    # A fresh local GROUP row is then read by the active HFC
                    # Planner through the real row loader while pending replay
                    # IDs remain excluded and unacknowledged.
                    await send_online(
                        source_chat, "source", is_group=True,
                        message_id="online-local-group", text="safe local group message",
                    )
                    await asyncio.wait_for(hfc_plan_called.wait(), timeout=2.0)
                    await asyncio.wait_for(turn_finished["source_local"].wait(), timeout=2.0)
                    self.assertEqual(len(hfc_planner.plan_calls), 1)
                    group_trigger = hfc_planner.plan_calls[0]["trigger_messages"]
                    self.assertEqual([message.processed_plain_text for message in group_trigger], ["safe local group message"])

                    # A current private message keeps its exact online metadata,
                    # triggers the real priority switch/CAS path, starts the
                    # target Brain managed loop, and reaches its Planner.
                    await send_online(
                        private_chat, "peer", is_group=False,
                        message_id="online-private-message", text="safe private text",
                    )
                    await asyncio.wait_for(brain_plan_called.wait(), timeout=3.0)
                    await asyncio.wait_for(turn_finished["private"].wait(), timeout=2.0)
                    private_turn = next(turn for turn in turns if turn.lease.chat_id == private_chat)
                    self.assertIsNone(private_turn.recovery_batch)
                    self.assertEqual([message.processed_plain_text for message in brain_planner.plan_calls[0]["trigger_messages"]], ["safe private text"])
                    self.assertEqual(coordinator.current_lease(private_chat).chat_id, private_chat)
                    self.assertTrue(coordinator._groups[group_id].recovery_collecting)

                    # Neither ordinary cursor commit nor the real switch ACKs
                    # any of the durable history rows or changes their times.
                    self.assertEqual(coordinator._groups[group_id].committed_cursor[source_chat], history_rows["live-online-local-group"])
                    self.assertEqual(
                        {row["row_id"] for row in store.pending_rows()},
                        {history_rows["old-source"], history_rows["current-source"], history_rows["old-private"], history_rows["replay-source"]},
                    )
                    pending_by_id = {row["row_id"]: row["original_timestamp"] for row in store.pending_rows()}
                    self.assertEqual(pending_by_id[history_rows["old-source"]], old_source_time)
                    self.assertEqual(pending_by_id[history_rows["current-source"]], current_source_time)
                    self.assertEqual(pending_by_id[history_rows["old-private"]], old_private_time)
                    self.assertEqual(pending_by_id[history_rows["replay-source"]], now - 4_000.0)
                    self.assertTrue(store.history_target_blocked(current_run_id, "group", "source"))
                    self.assertFalse(coordinator._groups[group_id].recovery_queue)

                    # Stop only the controlled test loops, then genuinely seal
                    # the scan and let the real quiet collector freeze batches.
                    for task in loop_tasks:
                        task.cancel()
                    await asyncio.gather(*loop_tasks, return_exceptions=True)
                    loop_tasks.clear()
                    manager.quiet_seconds = 0.1
                    await manager.handle_history_run_message({"content": {**current_payload, "phase": "seal"}})
                    await manager.handle_progress_message({"content": {**heartbeat, "active": False}})
                    collection_task = manager._collections[f"focus:{group_id}"].task
                    await asyncio.wait_for(collection_task, timeout=2.0)
                    self.assertFalse(coordinator._groups[group_id].recovery_collecting)
                    queue = coordinator._groups[group_id].recovery_queue
                    self.assertEqual([batch.chat_id for batch in queue], [private_chat, source_chat])
                    source_batch = next(batch for batch in queue if batch.chat_id == source_chat)
                    self.assertEqual(
                        source_batch.row_ids,
                        (history_rows["old-source"], history_rows["replay-source"], history_rows["current-source"]),
                    )
                    self.assertEqual(
                        source_batch.row_timestamps,
                        (old_source_time, now - 4_000.0, current_source_time),
                    )
                    recovery_turn = await asyncio.wait_for(coordinator.wait_for_turn(private_chat), timeout=1.0)
                    self.assertEqual(recovery_turn.recovery_batch, queue[0])
                    self.assertEqual(recovery_turn.recovery_batch.chat_id, private_chat)
                    second_turn = asyncio.create_task(coordinator.wait_for_turn(private_chat))
                    try:
                        await asyncio.sleep(0.02)
                        self.assertFalse(second_turn.done(), "Focus dispatched more than one recovery turn at once")
                    finally:
                        second_turn.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await second_turn

            finally:
                coordinator.set_ensure_runtime_callback(None)
                for task in loop_tasks:
                    task.cancel()
                if loop_tasks:
                    await asyncio.gather(*loop_tasks, return_exceptions=True)
                await manager.shutdown()
                store.close()
                database.drop_tables([Messages])
                database.close()

    @staticmethod
    def _stream(chat_id: str, *, group_id: str | None = None, group_name: str = "", user_id: str = ""):
        group_info = SimpleNamespace(group_id=group_id, group_name=group_name, group_platform="qq") if group_id else None
        user_info = SimpleNamespace(user_id=user_id, user_nickname="Private peer", user_cardname="") if user_id else None
        return SimpleNamespace(
            stream_id=chat_id,
            platform="qq",
            group_info=group_info,
            context=SimpleNamespace(get_template_name=Mock(return_value=None)),
            user_info=user_info,
        )

    @staticmethod
    def _runtime(runtime_type, chat_id: str, stream, planner: _Planner):
        runtime = object.__new__(runtime_type)
        runtime.stream_id = chat_id
        runtime.log_prefix = f"[{chat_id}]"
        runtime.chat_stream = stream
        runtime.running = False
        runtime._loop_task = None
        runtime._in_flight_operations = 0
        runtime._cycle_counter = 0
        runtime._current_cycle_detail = None
        runtime.history_loop = []
        runtime.last_read_time = time.time() - 60.0
        runtime._last_message_received_at = time.time()
        runtime.no_reply_until_call = False
        runtime.talk_threshold = 1.0
        runtime.blocked_users = {}
        runtime.more_plan = False
        runtime.expression_learner = SimpleNamespace(trigger_learning_for_chat=AsyncMock())
        runtime.action_modifier = SimpleNamespace(modify_actions=AsyncMock())
        runtime.action_manager = SimpleNamespace(get_using_actions=Mock(return_value={}))
        runtime.action_planner = planner
        runtime._planner_interrupt_flag = None
        runtime._planner_interrupt_requested = False
        runtime._planner_interrupt_consecutive_count = 0
        runtime._focus_turn_interrupted = False
        runtime._focus_delivered_event_revisions = None
        runtime._offline_recovery_batch = None
        runtime._offline_recovery_messages = []
        runtime._offline_recovery_prompt = ""
        runtime._offline_recovery_delivered = False
        runtime._offline_recovery_dropped = False
        runtime._focus_bypass_gate = None
        runtime.start_cycle = Mock(return_value=({}, f"cycle-{chat_id}"))
        runtime.end_cycle = Mock()
        runtime.print_cycle_info = Mock()
        runtime._focus_reply_context = Mock(return_value=None)
        return runtime

    @staticmethod
    def _create_row(
        chat_id: str,
        message_id: str,
        text: str,
        timestamp: float,
        *,
        group_id: str | None = None,
        user_id: str = "sender",
        additional_config: dict | None = None,
    ) -> int:
        row = Messages.create(
            message_id=message_id,
            time=timestamp,
            chat_id=chat_id,
            chat_info_stream_id=chat_id,
            chat_info_platform="qq",
            chat_info_user_platform="qq",
            chat_info_user_id=user_id,
            chat_info_user_nickname="Sender",
            chat_info_user_cardname="",
            chat_info_group_platform="qq" if group_id else None,
            chat_info_group_id=group_id,
            chat_info_group_name="Test group" if group_id else None,
            chat_info_create_time=timestamp,
            chat_info_last_active_time=timestamp,
            user_platform="qq",
            user_id=user_id,
            user_nickname="Sender",
            user_cardname="",
            processed_plain_text=text,
            display_message=text,
            additional_config=json.dumps(additional_config or {}),
            is_mentioned=False,
            is_at=False,
            is_command=False,
        )
        return int(row.id)

    @staticmethod
    def _claim_pending(
        store: HistoryDeliveryStore,
        run_id: str,
        key: str,
        message_id: str,
        kind: str,
        session_id: str,
        row_id: int,
        original_timestamp: float,
        now: float,
    ) -> None:
        runs = {run["run_id"]: run for run in store.open_history_runs()}
        run = runs[run_id]
        status = store.claim(
            {
                "delivery_key": key,
                "backend": "napcat",
                "account_id": "isolated-test-account",
                "platform": "qq",
                "session_kind": kind,
                "session_id": session_id,
                "message_id": message_id,
                "recovered": True,
                "original_timestamp": original_timestamp,
                "history_run": {"run_id": run_id, "cutoff_at": run["cutoff_at"]},
            },
            now=now,
        )
        if status != "new":
            raise AssertionError(f"isolated history delivery claim failed: {status}")
        store.mark_pending(key, chat_id=f"qq:{kind}:{session_id}", row_id=row_id, original_timestamp=original_timestamp)

    @staticmethod
    def _incoming_message(chat_stream, external_id: str, is_group: bool, message_id: str, text: str, metadata: dict):
        user_info = SimpleNamespace(user_id="sender", user_nickname="Sender", user_cardname="")
        group_info = (
            SimpleNamespace(group_id=external_id, group_name=getattr(chat_stream.group_info, "group_name", "Group"))
            if is_group
            else None
        )
        return SimpleNamespace(
            message_info=SimpleNamespace(
                platform="qq",
                message_id=message_id,
                time=metadata["offline_recovery"]["original_timestamp"],
                user_info=user_info,
                group_info=group_info,
                additional_config=metadata,
            ),
            chat_stream=chat_stream,
            processed_plain_text=text,
            is_picid=False,
            is_emoji=False,
            is_voice=False,
            is_video=False,
            is_mentioned=False,
            is_at=False,
            is_notify=False,
            is_command=False,
        )


class _TemporaryDirectory:
    def __init__(self):
        import tempfile

        self._directory = tempfile.TemporaryDirectory(prefix="focus-live-recovery-")

    def __enter__(self):
        return self._directory.name

    def __exit__(self, *args):
        return self._directory.__exit__(*args)
