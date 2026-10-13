from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class _NullLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


_MISSING = object()
_STUB_MODULE_NAMES = (
    "src.chat",
    "src.chat.message_receive",
    "src.chat.message_receive.chat_stream",
    "src.common.logger",
)
_PARENT_MODULE_ATTRIBUTES = (
    ("src", "chat"),
    ("src.chat", "message_receive"),
    ("src.chat.message_receive", "chat_stream"),
    ("src.common", "logger"),
)


def _install_non_starting_import_stubs():
    """Keep these source-level tests out of Core config, DB, and log startup."""

    source_root = Path(__file__).resolve().parents[1] / "src"
    prior_modules = {name: sys.modules.get(name, _MISSING) for name in _STUB_MODULE_NAMES}
    prior_attributes = {}
    for parent_name, attribute in _PARENT_MODULE_ATTRIBUTES:
        parent = sys.modules.get(parent_name, _MISSING)
        prior_attributes[(parent_name, attribute)] = (
            parent,
            getattr(parent, attribute, _MISSING) if parent is not _MISSING else _MISSING,
        )

    chat_package = types.ModuleType("src.chat")
    chat_package.__path__ = [str(source_root / "chat")]

    receive_package = types.ModuleType("src.chat.message_receive")
    receive_package.__path__ = [str(source_root / "chat" / "message_receive")]

    stream_module = types.ModuleType("src.chat.message_receive.chat_stream")

    def no_live_chat_manager():
        return None

    stream_module.get_chat_manager = no_live_chat_manager
    sys.modules["src.chat.message_receive.chat_stream"] = stream_module

    logger_module = types.ModuleType("src.common.logger")

    def no_op_logger(name=None):
        return _NullLogger()

    logger_module.get_logger = no_op_logger
    stubs = {
        "src.chat": chat_package,
        "src.chat.message_receive": receive_package,
        "src.chat.message_receive.chat_stream": stream_module,
        "src.common.logger": logger_module,
    }
    sys.modules.update(stubs)
    return prior_modules, prior_attributes, stubs


def _restore_non_starting_import_stubs(snapshot) -> None:
    prior_modules, prior_attributes, stubs = snapshot
    for name, prior in prior_modules.items():
        if prior is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prior

    for (parent_name, attribute), (prior_parent, prior_value) in prior_attributes.items():
        parent = sys.modules.get(parent_name)
        if parent is None:
            continue
        if prior_parent is not _MISSING and parent is prior_parent:
            if prior_value is _MISSING:
                if getattr(parent, attribute, _MISSING) is stubs.get(f"{parent_name}.{attribute}"):
                    delattr(parent, attribute)
            else:
                setattr(parent, attribute, prior_value)
            continue
        if getattr(parent, attribute, _MISSING) is stubs.get(f"{parent_name}.{attribute}"):
            delattr(parent, attribute)


_IMPORT_STUB_SNAPSHOT = _install_non_starting_import_stubs()
_IMPORT_STUBS = _IMPORT_STUB_SNAPSHOT[2]


def _load_focus_api():
    from src.chat.focus.coordinator import FocusCoordinator
    from src.chat.focus.models import (
        ChatKind,
        FocusGroupDefinition,
        FocusGroupPhase,
        FocusMember,
        FocusRecoveryBatch,
        FocusStoppedError,
        StoredMessageRef,
        TurnOutcome,
        TurnStatus,
        WakeReason,
    )

    return (
        FocusCoordinator,
        ChatKind,
        FocusGroupDefinition,
        FocusGroupPhase,
        FocusMember,
        FocusRecoveryBatch,
        FocusStoppedError,
        StoredMessageRef,
        TurnOutcome,
        TurnStatus,
        WakeReason,
    )


(
    FocusCoordinator,
    ChatKind,
    FocusGroupDefinition,
    FocusGroupPhase,
    FocusMember,
    FocusRecoveryBatch,
    FocusStoppedError,
    StoredMessageRef,
    TurnOutcome,
    TurnStatus,
    WakeReason,
) = _load_focus_api()


def _load_offline_recovery_source():
    """Load the real manager while keeping Core's startup-owning chat stream out."""

    module_name = "offline_recovery_source_under_test"
    source_path = Path(__file__).resolve().parents[1] / "src" / "chat" / "offline_recovery.py"
    chat_stream_name = "src.chat.message_receive.chat_stream"
    chat_stream_stub = types.ModuleType(chat_stream_name)
    chat_stream_stub.get_chat_manager = lambda: None
    existing_chat_stream = sys.modules.get(chat_stream_name)
    sys.modules[chat_stream_name] = chat_stream_stub
    try:
        spec = importlib.util.spec_from_file_location(module_name, source_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load offline recovery source from {source_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if existing_chat_stream is None:
            sys.modules.pop(chat_stream_name, None)
        else:
            sys.modules[chat_stream_name] = existing_chat_stream


offline_recovery = _load_offline_recovery_source()
DatabaseMessages = __import__(
    "src.common.data_models.database_data_model",
    fromlist=["DatabaseMessages"],
).DatabaseMessages
_restore_non_starting_import_stubs(_IMPORT_STUB_SNAPSHOT)


class _StreamResolver:
    @staticmethod
    def get_stream_id(platform: str, external_id: str, *, is_group: bool) -> str:
        return f"{platform}:{'group' if is_group else 'private'}:{external_id}"


class _ObservedCoordinator(FocusCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.collection_finished = asyncio.Event()
        self.finished_batches = []

    async def finish_recovery_collection(self, group_id, batches=()):
        await super().finish_recovery_collection(group_id, batches)
        self.finished_batches.append((group_id, tuple(batches)))
        self.collection_finished.set()


class OfflineRecoveryCollectorTests(unittest.IsolatedAsyncioTestCase):
    def test_import_bootstrap_restores_existing_runtime_module_identities(self):
        sentinels = {name: types.ModuleType(name) for name in _STUB_MODULE_NAMES}
        with patch.dict(sys.modules, sentinels):
            snapshot = _install_non_starting_import_stubs()
            self.assertTrue(any(sys.modules[name] is not sentinels[name] for name in sentinels))
            _restore_non_starting_import_stubs(snapshot)
            for name, sentinel in sentinels.items():
                self.assertIs(sys.modules[name], sentinel, name)

        for name, stub in _IMPORT_STUBS.items():
            self.assertIsNot(sys.modules.get(name), stub, name)

    def setUp(self) -> None:
        self.coordinator = _ObservedCoordinator()
        self.source_chat = "qq:group:source"
        self.target_chat = "qq:group:target"
        self.group_id = "collector-focus-group"
        self.coordinator.register_group(
            FocusGroupDefinition(
                self.group_id,
                (
                    FocusMember(self.source_chat, ChatKind.GROUP),
                    FocusMember(self.target_chat, ChatKind.GROUP),
                ),
                initial_chat_id=self.source_chat,
            )
        )
        self.manager = offline_recovery.OfflineRecoveryManager(
            quiet_seconds=0.1,
            stale_progress_seconds=2.0,
        )
        self.coordinator_patch = patch.object(offline_recovery, "focus_coordinator", self.coordinator)
        self.stream_patch = patch.object(offline_recovery, "get_chat_manager", return_value=_StreamResolver())
        self.coordinator_patch.start()
        self.stream_patch.start()

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.stream_patch.stop()
        self.coordinator_patch.stop()

    def _metadata(
        self,
        progress_id: str,
        original_timestamp: float,
        *,
        source_instance: str = "adapter-process-a",
        connection_epoch: float = 2_000.0,
    ) -> dict:
        return {
            "version": 1,
            "source_instance": source_instance,
            "connection_epoch_started_at": connection_epoch,
            "received_at": 2_100.0,
            "original_timestamp": original_timestamp,
            "timestamp_source": "platform_seconds",
            "progress_id": progress_id,
        }

    def _progress(self, metadata: dict, active: bool) -> dict:
        return {
            "content": {
                **metadata,
                "platform": "qq",
                "group_id": "target",
                "user_id": None,
                "active": active,
            }
        }

    def _history_run_payload(
        self,
        run_id: str,
        *,
        source_instance: str = "adapter-process-a",
        cutoff_at: float = 2_500.0,
        targets: list[dict] | None = None,
        phase: str = "begin",
    ) -> dict:
        return {
            "version": 1,
            "backend": "napcat",
            "account_id": "bot-123",
            "platform": "qq",
            "source_instance": source_instance,
            "run_id": run_id,
            "run_token": f"token-{run_id}",
            "cutoff_at": cutoff_at,
            "phase": phase,
            "targets": targets or [{"session_kind": "group", "session_id": "target"}],
        }

    def _history_progress(self, run: dict, session_id: str, progress_id: str, active: bool, **overrides) -> dict:
        route_kind = next(
            (
                target["session_kind"]
                for target in run["targets"]
                if target["session_id"] == session_id
            ),
            "group",
        )
        content = {
            "version": 1,
            "platform": run["platform"],
            "source_instance": run["source_instance"],
            "progress_id": progress_id,
            "active": active,
            "connection_epoch_started_at": 2_000.0,
            "received_at": 2_100.0,
            "history_run": {"run_id": run["run_id"], "cutoff_at": run["cutoff_at"]},
            "route_total": len(run["targets"]),
            "group_id": session_id if route_kind == "group" else None,
            "user_id": session_id if route_kind == "private" else None,
        }
        content.update(overrides)
        return {"content": content}

    def _envelope(self, metadata: dict) -> dict:
        return {
            "message_info": {
                "platform": "qq",
                "user_info": {"user_id": "sender", "user_nickname": "Sender"},
                "group_info": {"group_id": "target"},
                "additional_config": {"offline_recovery": metadata},
            }
        }

    def _message(self, metadata: dict, *, system_event: bool = False, sender: bool = True):
        additional_config = {"offline_recovery": metadata}
        if system_event:
            additional_config["system_event"] = {"version": 1, "type": "qq.notice"}
        user_info = SimpleNamespace(user_id="sender", user_nickname="Sender") if sender else None
        return SimpleNamespace(
            message_info=SimpleNamespace(
                platform="qq",
                user_info=user_info,
                additional_config=additional_config,
            ),
            processed_plain_text="admitted replay text",
        )

    async def _send_replay(self, row_id: int, original_timestamp: float, *, message_time: float = 9_000_000.0):
        metadata = self._metadata(f"progress-{row_id}", original_timestamp)
        await self.manager.handle_progress_message(self._progress(metadata, True))
        ticket = self.manager.message_started_from_envelope(self._envelope(metadata))
        self.assertIsNotNone(ticket)
        message = self._message(metadata)
        stored = StoredMessageRef(row_id, self.target_chat, f"message-{row_id}", message_time)
        self.assertTrue(self.manager.record_stored_message(message, stored))
        self.manager.message_finished(ticket)
        await self.manager.handle_progress_message(self._progress(metadata, False))

    async def _send_nonfocus_replay(self, row_id: int, progress_id: str, original_timestamp: float):
        metadata = self._metadata(progress_id, original_timestamp)
        raw_progress = {
            "content": {
                **metadata,
                "platform": "qq",
                "group_id": "standalone",
                "user_id": None,
                "active": True,
            }
        }
        await self.manager.handle_progress_message(raw_progress)
        envelope = self._envelope(metadata)
        envelope["message_info"]["group_info"]["group_id"] = "standalone"
        ticket = self.manager.message_started_from_envelope(envelope)
        chat_id = "qq:group:standalone"
        message = self._message(metadata)
        message.message_id = f"platform-message-{row_id}"
        stored_ref = StoredMessageRef(row_id, chat_id, message.message_id, original_timestamp)
        self.assertTrue(self.manager.record_stored_message(message, stored_ref))
        self.manager.message_finished(ticket)
        raw_progress["content"]["active"] = False
        await self.manager.handle_progress_message(raw_progress)
        return chat_id, metadata, message.message_id

    async def _wait_for_collection(self) -> tuple:
        await asyncio.wait_for(self.coordinator.collection_finished.wait(), timeout=1.5)
        self.assertTrue(self.coordinator.finished_batches)
        return self.coordinator.finished_batches[-1][1]

    async def test_freezes_thirty_out_of_order_rows_using_metadata_minimum_and_one_batch(self):
        timestamps = {row_id: float(2_000 + ((row_id * 7) % 31)) for row_id in range(1, 31)}
        timestamps[17] = 101.0

        for row_id in range(30, 0, -1):
            await self._send_replay(row_id, timestamps[row_id])

        batches = await self._wait_for_collection()
        self.assertEqual(len(batches), 1)
        batch = batches[0]
        self.assertEqual(batch.chat_id, self.target_chat)
        self.assertEqual(batch.row_ids, tuple(sorted(timestamps, key=lambda row: (timestamps[row], row))))
        self.assertEqual(batch.min_original_timestamp, min(timestamps.values()))
        self.assertEqual(batch.min_original_timestamp, timestamps[17])
        self.assertEqual(
            self.coordinator.pending_recovery_row_ids_for_chat(self.target_chat),
            set(range(1, 31)),
        )

    async def test_adapter_and_core_progress_both_hold_the_freeze_until_completion(self):
        active_seen = asyncio.Event()
        original_active_check = self.manager._active_progress_for

        def observe_active(group_key: str) -> bool:
            active = original_active_check(group_key)
            if active:
                active_seen.set()
            return active

        with patch.object(self.manager, "_active_progress_for", side_effect=observe_active):
            metadata = self._metadata("slow-conversion", 100.0)
            await self.manager.handle_progress_message(self._progress(metadata, True))

            # The collector reaches its quiet-window check while only the
            # adapter conversion marker is open.
            await asyncio.wait_for(active_seen.wait(), timeout=1.0)
            self.assertTrue(self.manager.is_held(self.target_chat))
            self.assertFalse(self.coordinator.collection_finished.is_set())

            # Core starts tracking from the raw envelope before the slow hook;
            # closing the adapter marker must not release the collector.
            ticket = self.manager.message_started_from_envelope(self._envelope(metadata))
            self.assertIsNotNone(ticket)
            message = self._message(metadata)
            self.assertTrue(
                self.manager.record_stored_message(
                    message,
                    StoredMessageRef(1, self.target_chat, "slow-message", 100.0),
                )
            )
            active_seen.clear()
            await self.manager.handle_progress_message(self._progress(metadata, False))
            await asyncio.wait_for(active_seen.wait(), timeout=1.0)
            self.assertTrue(self.manager.is_held(self.target_chat))
            self.assertFalse(self.coordinator.collection_finished.is_set())

            self.manager.message_finished(ticket)
            batches = await self._wait_for_collection()
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0].row_ids, (1,))

    async def test_only_replay_progress_can_hold_or_extend_the_quiet_window(self):
        replay = self._metadata("quiet-replay", 100.0)
        await self.manager.handle_progress_message(self._progress(replay, True))
        ticket = self.manager.message_started_from_envelope(self._envelope(replay))
        self.assertIsNotNone(ticket)
        self.assertTrue(
            self.manager.record_stored_message(
                self._message(replay),
                StoredMessageRef(1, self.target_chat, "quiet-replay-row", 100.0),
            )
        )
        self.manager.message_finished(ticket)
        await self.manager.handle_progress_message(self._progress(replay, False))

        now = time.time()
        held_live = {
            **self._metadata("long-live-conversion", now),
            "connection_epoch_started_at": now - 10,
            "received_at": now,
        }
        await self.manager.handle_progress_message(self._progress(held_live, True))
        live_state = self.manager._progress[("qq", "adapter-process-a", "long-live-conversion")]
        self.assertFalse(live_state.offline_candidate)

        async def continue_live_traffic():
            for index in range(30):
                live_now = time.time()
                metadata = {
                    **self._metadata(f"live-{index}", live_now),
                    "connection_epoch_started_at": live_now - 10,
                    "received_at": live_now,
                }
                await self.manager.handle_progress_message(self._progress(metadata, True))
                await self.manager.handle_progress_message(self._progress(metadata, False))
                await asyncio.sleep(0.02)

        traffic = asyncio.create_task(continue_live_traffic())
        try:
            await asyncio.wait_for(self.coordinator.collection_finished.wait(), timeout=0.8)
            self.assertFalse(traffic.done(), "recovery should freeze while live traffic is still arriving")
            self.assertTrue(live_state.adapter_active, "a long-held live conversion must not block freezing")
            self.assertEqual(self.coordinator.finished_batches[-1][1][0].row_ids, (1,))
        finally:
            await traffic
            await self.manager.handle_progress_message(self._progress(held_live, False))

    async def test_empty_filtered_replay_collection_wakes_deferred_live_chat(self):
        chat_id = "qq:group:standalone"
        replay = self._metadata("filtered-replay", 100.0)
        replay_progress = {
            "content": {
                **replay,
                "platform": "qq",
                "group_id": "standalone",
                "user_id": None,
                "active": True,
            }
        }
        await self.manager.handle_progress_message(replay_progress)

        live_time = time.time()
        live_message = SimpleNamespace(
            message_info=SimpleNamespace(
                user_info=SimpleNamespace(user_id="42"),
                additional_config={},
            ),
            message_id="held-live-message",
        )
        self.assertTrue(
            self.manager.record_deferred_live_message(
                live_message,
                StoredMessageRef(9, chat_id, "held-live-message", live_time),
            )
        )

        woken = asyncio.Event()
        wake_calls = []

        async def record_wake(target_chat_id):
            wake_calls.append(target_chat_id)
            woken.set()

        wake_patch = patch.object(self.manager, "_wake_nonfocus", side_effect=record_wake)
        wake_patch.start()
        try:
            replay_progress["content"]["active"] = False
            await self.manager.handle_progress_message(replay_progress)
            await asyncio.wait_for(woken.wait(), timeout=1.0)
        finally:
            wake_patch.stop()

        self.assertEqual(wake_calls, [chat_id])
        self.assertEqual(self.manager._nonfocus_batches.get(chat_id), None)
        self.assertEqual(self.manager._deferred_live_rows[chat_id], {9: {"message:held-live-message"}})

    async def test_core_startup_fallback_uses_original_envelope_time_and_preserves_existing_config(self):
        envelope = {
            "message_info": {
                "platform": "qq",
                "time": 100.0,
                "user_info": {"user_id": "sender", "user_nickname": "Sender"},
                "group_info": {"group_id": "target"},
                "additional_config": {"adapter_flag": {"keep": True}},
            }
        }

        with patch.object(offline_recovery, "get_core_startup_boundary", return_value=2_000.0):
            ticket = self.manager.message_started_from_envelope(envelope)

        self.assertIsNotNone(ticket)
        additional_config = envelope["message_info"]["additional_config"]
        self.assertEqual(additional_config["adapter_flag"], {"keep": True})
        metadata = additional_config["offline_recovery"]
        self.assertEqual(metadata["original_timestamp"], 100.0)
        self.assertEqual(metadata["connection_epoch_started_at"], 2_000.0)
        self.assertEqual(metadata["detection_boundary"], "core_startup_fallback")

        message = SimpleNamespace(
            message_info=SimpleNamespace(
                platform="qq",
                user_info=SimpleNamespace(user_id="sender", user_nickname="Sender"),
                additional_config=additional_config,
            ),
            processed_plain_text="fallback replay",
        )
        self.assertTrue(
            self.manager.record_stored_message(
                message,
                StoredMessageRef(1, self.target_chat, "fallback-row", 100.0),
            )
        )
        self.manager.message_finished(ticket)
        batches = await self._wait_for_collection()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].row_ids, (1,))

    async def test_senderless_system_event_fallback_is_a_noop_and_preserves_its_envelope(self):
        additional_config = {
            "adapter_flag": "preserve",
            "system_event": {"version": 1, "type": "qq.notice"},
        }
        envelope = {
            "message_info": {
                "platform": "qq",
                "time": 100.0,
                "user_info": None,
                "group_info": {"group_id": "target"},
                "additional_config": dict(additional_config),
            }
        }

        ticket = self.manager.message_started_from_envelope(envelope)

        self.assertIsNone(ticket)
        self.assertEqual(envelope["message_info"]["additional_config"], additional_config)
        self.assertFalse(self.manager._collections)
        self.assertFalse(self.manager._progress)

    async def test_new_process_retires_old_markers_and_late_old_finish_cannot_release_new_work(self):
        old = self._metadata("old-progress", 100.0, source_instance="old-process")
        new = self._metadata("new-progress", 90.0, source_instance="new-process")
        await self.manager.handle_progress_message(self._progress(old, True))
        await self.manager.handle_progress_message(self._progress(new, True))

        old_key = ("qq", "old-process", "old-progress")
        new_key = ("qq", "new-process", "new-progress")
        self.assertFalse(self.manager._progress[old_key].adapter_active)
        self.assertTrue(self.manager._progress[new_key].adapter_active)

        await self.manager.handle_progress_message(self._progress(old, False))
        self.assertTrue(self.manager._progress[new_key].adapter_active)
        self.assertFalse(self.coordinator.collection_finished.is_set())

        await self.manager.handle_progress_message(self._progress(new, False))
        await self._wait_for_collection()
        self.assertFalse(self.manager.is_held(self.target_chat))

    async def test_same_process_socket_reconnect_keeps_pre_reconnect_conversion_barrier(self):
        old_socket = self._metadata(
            "queued-before-reconnect",
            100.0,
            source_instance="same-process",
            connection_epoch=2_000.0,
        )
        new_socket = self._metadata(
            "received-after-reconnect",
            90.0,
            source_instance="same-process",
            connection_epoch=2_200.0,
        )
        await self.manager.handle_progress_message(self._progress(old_socket, True))
        await self.manager.handle_progress_message(self._progress(new_socket, True))
        await self.manager.handle_progress_message(self._progress(new_socket, False))

        self.assertTrue(
            self.manager._progress[("qq", "same-process", "queued-before-reconnect")].adapter_active
        )
        self.assertFalse(self.coordinator.collection_finished.is_set())

        await self.manager.handle_progress_message(self._progress(old_socket, False))
        await self._wait_for_collection()
        self.assertFalse(self.manager.is_held(self.target_chat))

    async def test_senderless_system_events_and_non_replay_rows_never_enter_recovery_batch(self):
        metadata = self._metadata("event-progress", 100.0)
        await self.manager.handle_progress_message(self._progress(metadata, True))
        self.assertFalse(
            self.manager.record_stored_message(
                self._message(metadata, system_event=True),
                StoredMessageRef(1, self.target_chat, "system-event", 100.0),
            )
        )
        self.assertFalse(
            self.manager.record_stored_message(
                self._message(metadata, sender=False),
                StoredMessageRef(2, self.target_chat, "senderless", 100.0),
            )
        )
        non_replay = {**metadata, "original_timestamp": time.time() + 60.0}
        self.assertFalse(
            self.manager.record_stored_message(
                self._message(non_replay),
                StoredMessageRef(3, self.target_chat, "live-message", 100.0),
            )
        )
        await self.manager.handle_progress_message(self._progress(metadata, False))

        batches = await self._wait_for_collection()
        self.assertEqual(batches, ())
        self.assertFalse(self.manager.is_held(self.target_chat))
        self.assertFalse(self.coordinator.is_recovery_held(self.target_chat))

    async def test_context_filter_keeps_all_acknowledged_rows_from_one_session_batch(self):
        first = FocusRecoveryBatch("completed-batch", self.target_chat, (1,), 200.0)
        pending = FocusRecoveryBatch("pending-batch", self.target_chat, (3,), 100.0)
        await self.coordinator.finish_recovery_collection(self.group_id, (first, pending))
        session_batch = self.coordinator._groups[self.group_id].recovery_queue[0]
        self.assertEqual(session_batch.row_ids, (3, 1))

        source_turn = await self.coordinator.wait_for_turn(self.source_chat)
        switched = await self.coordinator.switch_to_recovery_head(source_turn.lease, session_batch)
        self.assertTrue(switched.success, switched.reason)
        target_turn = await self.coordinator.wait_for_turn(self.target_chat)
        self.assertEqual(target_turn.recovery_batch, session_batch)
        self.assertTrue(
            await self.coordinator.finish_turn(
                target_turn,
                TurnOutcome(
                    TurnStatus.COMPLETED,
                    recovery_delivered=True,
                ),
            )
        )

        completed_row = self._message(self._metadata("done", 200.0))
        completed_row.focus_row_id = 1
        pending_row = self._message(self._metadata("still-pending", 100.0))
        pending_row.focus_row_id = 3
        live_row = SimpleNamespace(focus_row_id=2, processed_plain_text="live message")

        filtered = self.manager.filter_recovery_rows_from_context(
            self.target_chat,
            (completed_row, pending_row, live_row),
        )
        self.assertEqual([row.focus_row_id for row in filtered], [1, 3, 2])
        self.assertIn(1, self.coordinator.recovered_row_ids_for_chat(self.target_chat))
        self.assertIn(3, self.coordinator.recovered_row_ids_for_chat(self.target_chat))
        self.assertEqual(self.coordinator.pending_recovery_row_ids_for_chat(self.target_chat), set())

    async def test_nonfocus_pending_batches_coalesce_around_a_claimed_head(self):
        wakeups: asyncio.Queue[str] = asyncio.Queue()

        async def observe_nonfocus_wake(chat_id: str) -> None:
            await wakeups.put(chat_id)

        chat_id = "qq:group:standalone"
        with patch.object(self.manager, "_wake_nonfocus", side_effect=observe_nonfocus_wake):
            await self._send_nonfocus_replay(1, "nonfocus-unclaimed-1", 100.0)
            self.assertEqual(await asyncio.wait_for(wakeups.get(), timeout=1.5), chat_id)
            first = self.manager.peek_nonfocus_batch(chat_id)
            self.assertIsNotNone(first)

            await self._send_nonfocus_replay(2, "nonfocus-unclaimed-2", 50.0)
            self.assertEqual(await asyncio.wait_for(wakeups.get(), timeout=1.5), chat_id)
            unclaimed_aggregate = self.manager._nonfocus_batches[chat_id]
            self.assertEqual(len(unclaimed_aggregate), 1)
            head = unclaimed_aggregate[0]
            self.assertNotEqual(head.batch_id, first.batch_id)
            self.assertEqual(head.row_ids, (2, 1))
            self.assertEqual(head.row_timestamps, (50.0, 100.0))
            self.assertEqual(head.min_original_timestamp, 50.0)
            self.assertTrue(self.manager.claim_nonfocus_batch(chat_id, head.batch_id))

            await self._send_nonfocus_replay(3, "nonfocus-claimed-3", 200.0)
            self.assertEqual(await asyncio.wait_for(wakeups.get(), timeout=1.5), chat_id)
            self.assertIs(self.manager._nonfocus_batches[chat_id][0], head)

            await self._send_nonfocus_replay(4, "nonfocus-claimed-4", 25.0)
            self.assertEqual(await asyncio.wait_for(wakeups.get(), timeout=1.5), chat_id)
            queue = self.manager._nonfocus_batches[chat_id]
            self.assertEqual(len(queue), 2)
            self.assertIs(queue[0], head)
            self.assertEqual(queue[1].row_ids, (4, 3))
            self.assertEqual(queue[1].row_timestamps, (25.0, 200.0))
            self.assertEqual(queue[1].min_original_timestamp, 25.0)
            self.assertIsNone(self.manager.peek_nonfocus_batch(chat_id))

            self.assertTrue(
                self.manager.finish_nonfocus_batch(
                    chat_id,
                    head.batch_id,
                    delivered=True,
                )
            )
            self.assertEqual(self.manager._nonfocus_recovered[chat_id], {1, 2})
            completed_row = DatabaseMessages(
                message_id="platform-message-1",
                chat_id=chat_id,
                additional_config=json.dumps(
                    {"offline_recovery": self._metadata("nonfocus-unclaimed-1", 100.0)}
                ),
            )
            self.assertEqual(self.manager.filter_recovered_nonfocus(chat_id, (completed_row,)), [])

            tail = self.manager.peek_nonfocus_batch(chat_id)
            self.assertIsNotNone(tail)
            self.assertEqual(tail.row_ids, (4, 3))
            self.assertTrue(self.manager.claim_nonfocus_batch(chat_id, tail.batch_id))
            self.assertTrue(
                self.manager.finish_nonfocus_batch(
                    chat_id,
                    tail.batch_id,
                    delivered=True,
                )
            )
            self.assertNotIn(chat_id, self.manager._nonfocus_batches)

    async def test_nonfocus_receipt_filters_database_messages_by_identity_without_row_ids(self):
        wake_nonfocus = asyncio.Event()

        async def observe_nonfocus_wake(chat_id: str) -> None:
            self.assertEqual(chat_id, "qq:group:standalone")
            wake_nonfocus.set()

        with patch.object(self.manager, "_wake_nonfocus", side_effect=observe_nonfocus_wake):
            chat_id, first_metadata, first_message_id = await self._send_nonfocus_replay(1, "nonfocus-1", 200.0)
            await asyncio.wait_for(wake_nonfocus.wait(), timeout=1.5)
            first_batch = self.manager.peek_nonfocus_batch(chat_id)
            self.assertIsNotNone(first_batch)
            self.assertEqual(first_batch.row_ids, (1,))

            self.assertFalse(
                self.manager.finish_nonfocus_batch(
                    chat_id,
                    first_batch.batch_id,
                    delivered=False,
                )
            )
            self.assertEqual(self.manager._nonfocus_batches[chat_id][0], first_batch)

            self.assertTrue(
                self.manager.finish_nonfocus_batch(
                    chat_id,
                    first_batch.batch_id,
                    delivered=True,
                )
            )

            wake_nonfocus.clear()
            chat_id2, pending_metadata, pending_message_id = await self._send_nonfocus_replay(
                3,
                "nonfocus-3",
                100.0,
            )
            self.assertEqual(chat_id2, chat_id)
            await asyncio.wait_for(wake_nonfocus.wait(), timeout=1.5)
            pending_batch = self.manager.peek_nonfocus_batch(chat_id)
            self.assertIsNotNone(pending_batch)
            self.assertEqual(pending_batch.row_ids, (3,))

        def database_message(message_id: str, row_id: int, metadata: dict | None, *, text: str):
            additional_config = (
                json.dumps({"offline_recovery": metadata}) if metadata is not None else json.dumps({"source": "live"})
            )
            return DatabaseMessages(
                message_id=message_id,
                time=100.0 + row_id,
                chat_id=chat_id,
                processed_plain_text=text,
                additional_config=additional_config,
                user_id="sender",
                user_platform="qq",
            )

        completed_db_row = database_message(first_message_id, 1, first_metadata, text="completed replay")
        live_db_row = database_message("live-message-2", 2, None, text="interleaved live")
        pending_db_row = database_message(pending_message_id, 3, pending_metadata, text="pending replay")
        for row in (completed_db_row, live_db_row, pending_db_row):
            self.assertFalse(hasattr(row, "id"))
            self.assertFalse(hasattr(row, "focus_row_id"))

        replay_only_window = (completed_db_row,)
        self.assertEqual(self.manager.filter_recovered_nonfocus(chat_id, replay_only_window), [])
        self.assertEqual(
            self.manager.filter_recovered_nonfocus(chat_id, replay_only_window),
            [],
            "a replay-only raw window must stay suppressed until the normal read cursor advances",
        )
        self.assertIn(1, self.manager._nonfocus_recovered[chat_id])

        recent = self.manager.filter_recovered_nonfocus(chat_id, (completed_db_row, live_db_row))
        self.assertEqual(recent, [live_db_row], "an acknowledged offline row re-entered normal recent/unread reads")
        self.assertIn(1, self.manager._nonfocus_recovered[chat_id])
        self.manager.commit_nonfocus_read_cursor(chat_id)
        self.assertNotIn(1, self.manager._nonfocus_recovered.get(chat_id, set()))

        context = self.manager.filter_recovery_rows_from_context(
            chat_id,
            (completed_db_row, live_db_row, pending_db_row),
        )
        self.assertEqual(
            context,
            [completed_db_row, live_db_row],
            "completed replay history should remain visible while the pending batch stays hidden",
        )

    async def test_core_restart_restores_open_run_hold_before_partial_session_can_reach_focus(self):
        class _RecordingFocus:
            def __init__(self):
                self.finished = []

            @staticmethod
            def recovery_group_for_chat(_chat_id):
                return "restart-history-focus"

            @staticmethod
            def begin_recovery_collection(_chat_id):
                return None

            async def finish_recovery_collection(self, group_id, batches=()):
                self.finished.append((group_id, tuple(batches)))

        temp = tempfile.TemporaryDirectory()
        store = offline_recovery.history_delivery_store.__class__(Path(temp.name) / "core-history.sqlite3")
        old_run = {
            "version": 1,
            "backend": "napcat",
            "account_id": "bot-123",
            "platform": "qq",
            "source_instance": "adapter-old",
            "run_id": "restart-old-run",
            "run_token": "restart-old-token",
            "cutoff_at": 1_780_000_200.0,
            "phase": "begin",
            "targets": [
                {"session_kind": "group", "session_id": "A"},
                {"session_kind": "group", "session_id": "B"},
            ],
        }
        store.begin_history_run(old_run)
        delivery_b = {
            "backend": "napcat",
            "account_id": "bot-123",
            "platform": "qq",
            "session_kind": "group",
            "session_id": "B",
            "message_id": "message-B",
            "delivery_key": "delivery-B",
            "receipt_token": "receipt-B",
            "recovered": True,
            "original_timestamp": 1_780_000_100.25,
            "history_run": {"run_id": "restart-old-run", "cutoff_at": 1_780_000_200.0},
        }
        self.assertEqual(store.claim(delivery_b), "new")
        store.mark_pending(
            delivery_b["delivery_key"],
            chat_id="qq:group:B",
            row_id=77,
            original_timestamp=1_780_000_100.25,
        )
        focus = _RecordingFocus()
        manager = offline_recovery.OfflineRecoveryManager(quiet_seconds=0.05, stale_progress_seconds=0.1)
        try:
            with (
                patch.object(offline_recovery, "history_delivery_store", store),
                patch.object(offline_recovery, "focus_coordinator", focus),
                patch.object(offline_recovery, "get_chat_manager", return_value=_StreamResolver()),
            ):
                self.assertEqual(manager.restore_open_history_runs(), 2)
                self.assertEqual(manager.restore_pending_deliveries(), 1)
                await asyncio.sleep(0.3)
                self.assertFalse(
                    manager._active_progress_for("focus:restart-history-focus"),
                    "restored open runs retain route isolation without creating live scan activity",
                )
                self.assertTrue(focus.finished, "the transient collection hold should finish after its lease expires")
                self.assertTrue(all(not batches for _group, batches in focus.finished))

                new_run = {
                    **old_run,
                    "source_instance": "adapter-new",
                    "run_id": "restart-new-run",
                    "run_token": "restart-new-token",
                }
                store.begin_history_run(new_run)
                manager._register_history_run_progress(
                    run_id=new_run["run_id"],
                    platform=new_run["platform"],
                    source_instance=new_run["source_instance"],
                    cutoff_at=new_run["cutoff_at"],
                    targets=new_run["targets"],
                )
                manager._retire_history_run_progress("qq", except_run_id=new_run["run_id"])

                delivery_a = {
                    **delivery_b,
                    "session_id": "A",
                    "message_id": "message-A",
                    "delivery_key": "delivery-A",
                    "receipt_token": "receipt-A",
                    "history_run": {"run_id": new_run["run_id"], "cutoff_at": new_run["cutoff_at"]},
                }
                self.assertEqual(store.claim(delivery_a), "new")
                store.mark_pending(
                    delivery_a["delivery_key"],
                    chat_id="qq:group:A",
                    row_id=78,
                    original_timestamp=1_780_000_100.5,
                )
                self.assertEqual(manager.restore_pending_deliveries([delivery_a["delivery_key"]]), 1)
                stale_empty_routes = store.release_empty_open_targets_from_older_instances(
                    backend="napcat",
                    account_id="bot-123",
                    platform="qq",
                    source_instance="adapter-new",
                )
                manager._release_closed_history_targets(stale_empty_routes)
                partial_seal = {
                    **new_run,
                    "phase": "seal",
                    "targets": [{"session_kind": "group", "session_id": "A"}],
                }
                closed = store.seal_history_run(partial_seal)
                manager._retire_history_run_progress("qq", except_run_id=None, only_run_id=new_run["run_id"])
                manager._release_closed_history_targets(closed)
                await asyncio.sleep(0.2)
                batches = [batch for _group, batch_group in focus.finished for batch in batch_group]
                self.assertEqual(len(batches), 1, "a failed sibling's durable hold must not block completed A")
                self.assertEqual(batches[0].chat_id, "qq:group:A")
                self.assertEqual(batches[0].row_ids, (78,))
                self.assertIn(77, manager._collections["focus:restart-history-focus"].rows["qq:group:B"])

                # Duplicate ACK while A is already queued/in flight must not
                # add another copy behind the current Focus head.
                self.assertEqual(manager.restore_pending_deliveries([delivery_a["delivery_key"]]), 0)
                self.assertEqual(len(manager._collections["focus:restart-history-focus"].rows.get("qq:group:A", {})), 0)

                full_seal = {**new_run, "phase": "seal", "targets": new_run["targets"]}
                closed = store.seal_history_run(full_seal)
                manager._retire_history_run_progress("qq", except_run_id=None, only_run_id=new_run["run_id"])
                manager._release_closed_history_targets(closed)
                await asyncio.sleep(0.3)
                batches = [batch for _group, batch_group in focus.finished for batch in batch_group]
                self.assertEqual([(batch.chat_id, batch.row_ids) for batch in batches], [
                    ("qq:group:A", (78,)),
                    ("qq:group:B", (77,)),
                ])
                self.assertEqual(manager.complete_rows([77, 78]), 2)
                self.assertEqual(store.state(delivery_a["delivery_key"]), "complete")
                self.assertEqual(store.state(delivery_b["delivery_key"]), "complete")
        finally:
            await manager.shutdown()
            store.close()
            temp.cleanup()

    async def test_restored_open_runs_keep_partial_private_rows_gated_without_holding_live_focus(self):
        private_chat = "qq:private:peer"
        focus = _ObservedCoordinator()
        focus_group_id = "private-history-recovery"
        focus.register_group(
            FocusGroupDefinition(
                focus_group_id,
                (
                    FocusMember(self.source_chat, ChatKind.GROUP),
                    FocusMember(private_chat, ChatKind.PRIVATE),
                ),
                initial_chat_id=self.source_chat,
            )
        )
        temp = tempfile.TemporaryDirectory()
        store = offline_recovery.history_delivery_store.__class__(Path(temp.name) / "private-history.sqlite3")
        targets = [{"session_kind": "private", "session_id": "peer"}]
        old_run = self._history_run_payload(
            "restart-private-old",
            source_instance="adapter-old",
            targets=targets,
        )
        current_run = self._history_run_payload(
            "restart-private-current",
            source_instance="adapter-current",
            targets=targets,
        )
        store.begin_history_run(old_run)
        store.begin_history_run(current_run)
        delivery = {
            "backend": "napcat",
            "account_id": "bot-123",
            "platform": "qq",
            "session_kind": "private",
            "session_id": "peer",
            "message_id": "partial-private-replay",
            "delivery_key": "partial-private-delivery",
            "receipt_token": "partial-private-receipt",
            "recovered": True,
            "original_timestamp": 2_100.25,
            "history_run": {
                "run_id": current_run["run_id"],
                "cutoff_at": current_run["cutoff_at"],
            },
        }
        self.assertEqual(store.claim(delivery), "new")
        store.mark_pending(
            delivery["delivery_key"],
            chat_id=private_chat,
            row_id=77,
            original_timestamp=delivery["original_timestamp"],
        )
        self.manager.quiet_seconds = 0.04
        self.manager.stale_progress_seconds = 0.1
        try:
            with (
                patch.object(offline_recovery, "history_delivery_store", store),
                patch.object(offline_recovery, "focus_coordinator", focus),
                patch.object(offline_recovery, "get_chat_manager", return_value=_StreamResolver()),
            ):
                self.assertEqual(self.manager.restore_open_history_runs(), 2)
                self.assertEqual(self.manager.restore_pending_deliveries(), 1)
                self.assertNotIn("qq", self.manager._latest_source_instance)
                await asyncio.wait_for(focus.collection_finished.wait(), timeout=1.0)

                group_key = f"focus:{focus_group_id}"
                self.assertFalse(self.manager._active_progress_for(group_key))
                self.assertFalse(focus._groups[focus_group_id].recovery_collecting)
                self.assertEqual(len(store.open_history_runs()), 2)
                self.assertTrue(
                    store.history_target_blocked(current_run["run_id"], "private", "peer")
                )
                hidden = DatabaseMessages(
                    message_id=delivery["message_id"],
                    chat_id=private_chat,
                    additional_config=json.dumps({"offline_recovery": {"delivery": {
                        "delivery_key": delivery["delivery_key"],
                    }}}),
                )
                self.assertEqual(
                    self.manager.filter_recovery_rows_from_context(private_chat, (hidden,)),
                    [],
                    "the incomplete recovered row stays out of ordinary private context",
                )
                self.assertTrue(
                    all(not batches for _group, batches in focus.finished_batches),
                    "an incomplete recovered row must not leak into a Focus batch",
                )

                live_message = SimpleNamespace(
                    chat_stream=SimpleNamespace(stream_id=private_chat),
                    message_info=SimpleNamespace(
                        platform="qq",
                        user_info=SimpleNamespace(user_id="sender", user_nickname="Sender"),
                        additional_config={},
                    ),
                    message_id="ordinary-private-live",
                    processed_plain_text="ordinary QQ private text",
                )
                dispatch = await focus.route_message(
                    live_message,
                    StoredMessageRef(201, private_chat, "ordinary-private-live", 2_200.0),
                )
                self.assertTrue(dispatch.woke_active)
                turn = await focus.wait_for_turn(self.source_chat)
                self.assertTrue(turn.wake_reason & WakeReason.FOCUS_EVENT)

                current_pulse = self._history_progress(
                    current_run,
                    "peer",
                    "current-private-route",
                    True,
                )
                await self.manager.handle_progress_message(current_pulse)
                self.assertEqual(self.manager._latest_source_instance["qq"], "adapter-current")
                self.assertIn(old_run["run_id"], self.manager._retired_history_runs)
                old_pulse = self._history_progress(old_run, "peer", "old-private-route", True)
                await self.manager.handle_progress_message(old_pulse)
                self.assertNotIn(("qq", "adapter-old", "old-private-route"), self.manager._progress)
                self.assertEqual(self.manager._latest_source_instance["qq"], "adapter-current")
                self.assertTrue(self.manager._active_progress_for(group_key))

                current_pulse["content"]["active"] = False
                await self.manager.handle_progress_message(current_pulse)
                self.assertFalse(self.manager._active_progress_for(group_key))
        finally:
            await self.manager.shutdown()
            store.close()
            temp.cleanup()

    async def test_history_run_heartbeats_refresh_matching_routes_and_false_releases_promptly(self):
        self.manager.quiet_seconds = 0.03
        self.manager.stale_progress_seconds = 0.14
        temp = tempfile.TemporaryDirectory()
        store = offline_recovery.history_delivery_store.__class__(Path(temp.name) / "history-heartbeats.sqlite3")
        run = self._history_run_payload(
            "heartbeat-run",
            targets=[
                {"session_kind": "group", "session_id": "target"},
                {"session_kind": "group", "session_id": "source"},
            ],
        )
        receipt_module = types.ModuleType("src.common.message")

        async def ignore_receipt(*_args, **_kwargs):
            return True

        receipt_module.get_global_api = lambda: SimpleNamespace(send_custom_message=ignore_receipt)
        try:
            with (
                patch.object(offline_recovery, "history_delivery_store", store),
                patch.dict(sys.modules, {"src.common.message": receipt_module}),
            ):
                await self.manager.handle_history_run_message({"content": run})
                target_hold = self._history_progress(run, "target", "route-target", True)
                await asyncio.sleep(0.04)
                await self.manager.handle_progress_message(target_hold)
                await asyncio.sleep(0.03)

                target_hold["content"]["active"] = False
                await self.manager.handle_progress_message(target_hold)
                self.assertTrue(
                    self.manager._active_progress_for(f"focus:{self.group_id}"),
                    "ending target's scan must leave the sibling source route active",
                )
                route_state = self.manager._progress[("qq", run["source_instance"], "route-target")]
                self.assertFalse(route_state.adapter_active)
                await asyncio.wait_for(self.coordinator.collection_finished.wait(), timeout=1.0)
                self.assertFalse(self.manager._active_progress_for(f"focus:{self.group_id}"))
                self.assertFalse(self.coordinator._groups[self.group_id].recovery_collecting)
                self.assertTrue(store.history_target_blocked(run["run_id"], "group", "target"))

                self.coordinator.collection_finished.clear()
                latest_source = self.manager._latest_source_instance["qq"]
                invalid_markers = (
                    self._history_progress(
                        run,
                        "target",
                        "foreign-run",
                        True,
                        history_run={"run_id": "unregistered-run", "cutoff_at": run["cutoff_at"]},
                    ),
                    self._history_progress(
                        run,
                        "target",
                        "foreign-source",
                        True,
                        source_instance="adapter-foreign",
                    ),
                    self._history_progress(run, "foreign-route", "foreign-route", True),
                    self._history_progress(
                        run,
                        "target",
                        "foreign-cutoff",
                        True,
                        history_run={"run_id": run["run_id"], "cutoff_at": run["cutoff_at"] + 1},
                    ),
                )
                for marker in invalid_markers:
                    await self.manager.handle_progress_message(marker)
                for marker in invalid_markers:
                    content = marker["content"]
                    self.assertNotIn(
                        ("qq", content["source_instance"], content["progress_id"]),
                        self.manager._progress,
                    )
                self.assertEqual(self.manager._latest_source_instance["qq"], latest_source)
                self.assertFalse(self.manager._active_progress_for(f"focus:{self.group_id}"))
                self.assertFalse(self.coordinator._groups[self.group_id].recovery_collecting)

                self.coordinator.collection_finished.clear()
                target_hold["content"]["active"] = True
                await self.manager.handle_progress_message(target_hold)
                self.assertTrue(self.coordinator._groups[self.group_id].recovery_collecting)
                self.assertTrue(self.manager._active_progress_for(f"focus:{self.group_id}"))
                await asyncio.sleep(0.09)
                await self.manager.handle_progress_message(target_hold)
                await asyncio.sleep(0.09)
                self.assertTrue(
                    self.manager._active_progress_for(f"focus:{self.group_id}"),
                    "the matching heartbeat renews both route leases past the original begin TTL",
                )

                target_hold["content"]["active"] = False
                await self.manager.handle_progress_message(target_hold)
                await asyncio.wait_for(self.coordinator.collection_finished.wait(), timeout=1.0)
                self.assertFalse(self.manager._active_progress_for(f"focus:{self.group_id}"))
                self.assertFalse(self.coordinator._groups[self.group_id].recovery_collecting)
                self.assertTrue(store.history_target_blocked(run["run_id"], "group", "target"))
        finally:
            await self.manager.shutdown()
            store.close()
            temp.cleanup()

    async def test_expired_begin_can_late_seal_pending_rows_once_and_finally_clears_uuid_marker(self):
        self.manager.quiet_seconds = 0.03
        self.manager.stale_progress_seconds = 0.12
        temp = tempfile.TemporaryDirectory()
        store = offline_recovery.history_delivery_store.__class__(Path(temp.name) / "late-history-seal.sqlite3")
        run = self._history_run_payload("late-seal-run")
        receipt_module = types.ModuleType("src.common.message")
        receipts = []

        async def record_receipt(*args, **kwargs):
            receipts.append((args, kwargs))
            return True

        receipt_module.get_global_api = lambda: SimpleNamespace(send_custom_message=record_receipt)
        deliveries = (
            ("delivery-late-77", "message-late-77", 77, 100.25),
            ("delivery-late-78", "message-late-78", 78, 95.0),
        )
        try:
            with (
                patch.object(offline_recovery, "history_delivery_store", store),
                patch.dict(sys.modules, {"src.common.message": receipt_module}),
            ):
                await self.manager.handle_history_run_message({"content": run})
                await asyncio.wait_for(self.coordinator.collection_finished.wait(), timeout=1.0)
                self.assertEqual(self.manager._collections, {}, "a begin without heartbeats must expire while its DB run stays open")
                self.assertTrue(store.history_target_blocked(run["run_id"], "group", "target"))
                self.coordinator.collection_finished.clear()

                for delivery_key, message_id, row_id, original_timestamp in deliveries:
                    delivery = {
                        "backend": "napcat",
                        "account_id": "bot-123",
                        "platform": "qq",
                        "session_kind": "group",
                        "session_id": "target",
                        "message_id": message_id,
                        "delivery_key": delivery_key,
                        "receipt_token": f"receipt-{row_id}",
                        "recovered": True,
                        "original_timestamp": original_timestamp,
                        "history_run": {"run_id": run["run_id"], "cutoff_at": run["cutoff_at"]},
                    }
                    self.assertEqual(store.claim(delivery), "new")
                    store.mark_pending(
                        delivery_key,
                        chat_id=self.target_chat,
                        row_id=row_id,
                        original_timestamp=original_timestamp,
                    )
                self.assertEqual(self.manager.restore_pending_deliveries(), 2)
                self.assertIn("focus:collector-focus-group", self.manager._collections)

                hold = self._history_progress(run, "target", "route-target-late", True)
                await self.manager.handle_progress_message(hold)
                seal = {**run, "phase": "seal"}
                await self.manager.handle_history_run_message({"content": seal})
                hold["content"]["active"] = False
                await self.manager.handle_progress_message(hold)
                uuid_state = self.manager._progress[("qq", run["source_instance"], "route-target-late")]
                self.assertFalse(uuid_state.adapter_active, "the valid finally marker closes UUID progress after seal")

                post_seal = self._history_progress(run, "target", "post-seal-active", True)
                await self.manager.handle_progress_message(post_seal)
                self.assertNotIn(("qq", run["source_instance"], "post-seal-active"), self.manager._progress)
                self.assertFalse(self.manager._active_progress_for("focus:collector-focus-group"))

                await self.manager.handle_history_run_message({"content": seal})
                requested_keys = [delivery_key for delivery_key, *_ in deliveries]
                self.assertEqual(self.manager.restore_pending_deliveries(requested_keys), 0)
                await asyncio.wait_for(self.coordinator.collection_finished.wait(), timeout=1.0)
                batches = [
                    batch
                    for _group, batch_group in self.coordinator.finished_batches
                    for batch in batch_group
                ]
                self.assertEqual(len(batches), 1)
                batch = batches[0]
                self.assertEqual(batch.chat_id, self.target_chat)
                self.assertEqual(batch.row_ids, (78, 77))
                self.assertEqual(batch.row_timestamps, (95.0, 100.25))

                turn = await self.coordinator.wait_for_turn(self.source_chat)
                self.assertIsNotNone(turn.recovery_batch)
                self.assertEqual(turn.recovery_batch.row_ids, (78, 77))
                self.assertEqual(turn.recovery_batch.row_timestamps, (95.0, 100.25))
                self.assertEqual(self.manager.complete_rows((78, 77)), 2)
                self.assertEqual(self.manager.complete_rows((78, 77)), 0)
                self.assertEqual(store.state("delivery-late-77"), "complete")
                self.assertEqual(store.state("delivery-late-78"), "complete")
                self.assertEqual(len(receipts), 3, "begin and repeated valid seals each receive one receipt")
        finally:
            await self.manager.shutdown()
            store.close()
            temp.cleanup()

    async def test_shutdown_drains_manager_timers_without_waking_stopping_focus(self):
        self.manager.quiet_seconds = 0.5
        metadata = self._metadata("shutdown-progress", 100.0)
        await self.manager.handle_progress_message(self._progress(metadata, True))
        ticket = self.manager.message_started_from_envelope(self._envelope(metadata))
        self.assertIsNotNone(ticket)
        self.assertTrue(self.manager.record_stored_message(
            self._message(metadata),
            StoredMessageRef(1, self.target_chat, "shutdown-message", 100.0),
        ))
        self.manager.message_finished(ticket)
        await self.manager.handle_progress_message(self._progress(metadata, False))
        self.assertTrue(self.manager._cleanup_tasks, "adapter completion did not schedule progress cleanup")
        self.assertTrue(self.manager._collections)

        live_during_hold = await self.coordinator.route_message(
            SimpleNamespace(chat_stream=SimpleNamespace(stream_id=self.source_chat)),
            StoredMessageRef(2, self.source_chat, "live-during-shutdown", 500.0),
        )
        self.assertTrue(live_during_hold.woke_active)
        await self.coordinator.begin_shutdown()
        state = self.coordinator._groups[self.group_id]
        self.assertIs(state.phase, FocusGroupPhase.STOPPING)

        await self.manager.shutdown()
        self.assertTrue(self.manager._stopping)
        self.assertEqual(self.manager._collections, {})
        self.assertEqual(self.manager._cleanup_tasks, set())
        self.assertTrue(state.recovery_collecting)
        self.assertTrue(state.pending_wakes.get(self.source_chat, WakeReason.NONE) & WakeReason.LOCAL_MESSAGE)

        stopped_dispatch = await self.coordinator.route_message(
            SimpleNamespace(chat_stream=SimpleNamespace(stream_id=self.source_chat)),
            StoredMessageRef(3, self.source_chat, "live-after-shutdown", 501.0),
        )
        self.assertFalse(stopped_dispatch.woke_active)

        with self.assertRaises(FocusStoppedError):
            await self.coordinator.wait_for_turn(self.source_chat)


if __name__ == "__main__":
    unittest.main()
