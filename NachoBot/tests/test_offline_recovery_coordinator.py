from __future__ import annotations

import asyncio
import importlib
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


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
    """Import the actual coordinator without initializing Core side effects."""

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
    coordinator_module = importlib.import_module("src.chat.focus.coordinator")
    models = importlib.import_module("src.chat.focus.models")
    scope_policy = importlib.import_module("src.chat.focus.scope_policy")
    return (
        coordinator_module,
        coordinator_module.FocusCoordinator,
        models.ChatKind,
        models.FocusGroupDefinition,
        models.FocusGroupPhase,
        models.FocusMember,
        models.FocusRecoveryBatch,
        models.HandoffKind,
        models.TurnOutcome,
        models.TurnStatus,
        models.StoredMessageRef,
        scope_policy.ChatScopePolicy,
    )


(
    coordinator_module,
    FocusCoordinator,
    ChatKind,
    FocusGroupDefinition,
    FocusGroupPhase,
    FocusMember,
    FocusRecoveryBatch,
    HandoffKind,
    TurnOutcome,
    TurnStatus,
    StoredMessageRef,
    ChatScopePolicy,
) = _load_focus_api()
_restore_non_starting_import_stubs(_IMPORT_STUB_SNAPSHOT)


class _Message:
    def __init__(self, chat_id: str, text: str = "message") -> None:
        self.chat_stream = SimpleNamespace(stream_id=chat_id)
        self.processed_plain_text = text


class _FailingCASStore:
    def __init__(self) -> None:
        self.cas_calls = 0

    async def upsert_event(self, *args, **kwargs) -> None:
        return None

    async def compare_and_set_switch(self, **kwargs) -> bool:
        self.cas_calls += 1
        return False


class _Clock:
    def __init__(self, now: float = 10.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    @staticmethod
    def time() -> float:
        return time.time()


class OfflineRecoveryCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_stubs_restore_preexisting_runtime_module_identities(self):
        sentinels = {name: types.ModuleType(name) for name in _STUB_MODULE_NAMES}
        with patch.dict(sys.modules, sentinels):
            snapshot = _install_non_starting_import_stubs()
            self.assertTrue(any(sys.modules[name] is not sentinels[name] for name in sentinels))
            _restore_non_starting_import_stubs(snapshot)
            for name, sentinel in sentinels.items():
                self.assertIs(sys.modules[name], sentinel, name)

        for name, stub in _IMPORT_STUBS.items():
            self.assertIsNot(sys.modules.get(name), stub, name)

    def _coordinator(self, *, state_store=None, policy=None) -> FocusCoordinator:
        return FocusCoordinator(state_store=state_store, policy=policy, unread_event_threshold=99)

    def _group(self, group_id: str, members: tuple[FocusMember, ...], active: str) -> FocusGroupDefinition:
        return FocusGroupDefinition(group_id, members, initial_chat_id=active)

    async def test_initial_latest_snapshot_wakes_explicit_unread_but_replay_does_not_extend_baseline(self):
        source = "qq:group:source"
        target = "qq:group:target"
        unread = self._coordinator()
        unread.register_group(
            self._group(
                "explicit-initial-unread",
                (FocusMember(source, ChatKind.GROUP), FocusMember(target, ChatKind.GROUP)),
                source,
            ),
            cursors={source: 0},
            latest_rows={source: 1},
        )
        initial_turn = await asyncio.wait_for(unread.wait_for_turn(source), timeout=0.5)
        self.assertTrue(initial_turn.wake_reason & coordinator_module.WakeReason.LOCAL_MESSAGE)
        self.assertEqual(initial_turn.read_through_row_id, 1)

        baseline = self._coordinator()
        baseline.register_group(
            self._group(
                "bootstrap-baseline",
                (FocusMember(source, ChatKind.GROUP), FocusMember(target, ChatKind.GROUP)),
                source,
            ),
            cursors={source: 5},
            latest_rows={source: 5},
        )
        group_id = "bootstrap-baseline"
        baseline.begin_recovery_collection(source)
        dispatch = await baseline.route_message(
            _Message(source, "recovery-only row after startup baseline"),
            StoredMessageRef(6, source, "recovered-row-6", 100.0),
            offline_recovery_row=True,
        )
        self.assertFalse(dispatch.woke_active)
        state = baseline._groups[group_id]
        self.assertEqual(state.latest_live_message_row[source], 5)
        self.assertFalse(state.pending_wakes.get(source, coordinator_module.WakeReason.NONE))

        waiter = asyncio.create_task(baseline.wait_for_turn(source))
        try:
            await asyncio.sleep(0.03)
            self.assertFalse(waiter.done(), "recovery-only ingress became ordinary startup eligibility")
        finally:
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter

    async def test_wait_for_turn_uses_selected_lease_for_handoff_lookup(self):
        source = "qq:group:source"
        target = "qq:group:target"
        observed = {}
        expected = {}

        for mode in ("ordinary", "collecting", "queued"):
            with self.subTest(mode=mode):
                group_id = f"handoff-{mode}"
                handoff = SimpleNamespace(handoff_id=f"{mode}-handoff")
                store = SimpleNamespace(get_active=AsyncMock(return_value=(handoff,)))
                coordinator = FocusCoordinator(handoff_store=store, unread_event_threshold=99)
                coordinator.register_group(
                    self._group(
                        group_id,
                        (FocusMember(source, ChatKind.GROUP), FocusMember(target, ChatKind.GROUP)),
                        source,
                    )
                )
                if mode == "collecting":
                    coordinator.begin_recovery_collection(source)
                elif mode == "queued":
                    batch = FocusRecoveryBatch("queued-head", target, (41, 42), 500.0)
                    await coordinator.finish_recovery_collection(group_id, (batch,))

                await coordinator.route_message(
                    _Message(source, f"live work during {mode}"),
                    StoredMessageRef(1, source, f"live-{mode}", 1_000.0),
                )
                queued_before = tuple(coordinator._groups[group_id].recovery_queue)
                turn = await asyncio.wait_for(coordinator.wait_for_turn(source), timeout=0.5)

                state = coordinator._groups[group_id]
                lookup_args = store.get_active.await_args
                scope = (turn.lease.group_id, turn.lease.chat_id, turn.lease.epoch)
                observed[mode] = (
                    store.get_active.await_count,
                    lookup_args.args if lookup_args is not None else None,
                    lookup_args.kwargs == {} if lookup_args is not None else False,
                    turn.recovery_batch,
                    turn.handoff_ids,
                    state.in_flight_turn == turn,
                    state.in_flight_turn.lease == turn.lease if state.in_flight_turn is not None else False,
                    state.in_flight_turn.handoff_ids if state.in_flight_turn is not None else (),
                    tuple(state.recovery_queue) == queued_before,
                    state.recovery_collecting == (mode == "collecting"),
                )
                expected[mode] = (
                    1,
                    scope,
                    True,
                    None,
                    (handoff.handoff_id,),
                    True,
                    True,
                    (handoff.handoff_id,),
                    True,
                    True,
                )

        group_id = "handoff-recovery"
        recovery_handoff = SimpleNamespace(handoff_id="recovery-handoff")
        recovery_store = SimpleNamespace(get_active=AsyncMock(return_value=(recovery_handoff,)))
        recovery = FocusCoordinator(handoff_store=recovery_store, unread_event_threshold=99)
        recovery.register_group(
            self._group(
                group_id,
                (FocusMember(source, ChatKind.GROUP), FocusMember(target, ChatKind.GROUP)),
                source,
            )
        )
        batch = FocusRecoveryBatch("recovery-head", target, (51, 52), 600.0)
        await recovery.finish_recovery_collection(group_id, (batch,))
        turn = await asyncio.wait_for(recovery.wait_for_turn(source), timeout=0.5)

        self.assertEqual(turn.recovery_batch, batch)
        self.assertEqual(turn.handoff_ids, (recovery_handoff.handoff_id,))
        recovery_store.get_active.assert_awaited_once_with(
            turn.lease.group_id,
            turn.lease.chat_id,
            turn.lease.epoch,
        )
        self.assertEqual(recovery._groups[group_id].in_flight_turn, turn)
        self.assertEqual(recovery._groups[group_id].recovery_queue, [batch])
        self.assertEqual(observed, expected)

    async def test_descending_minimum_order_serial_switches_and_deferred_private_event(self):
        source = "qq:group:source"
        newest = "qq:group:newest"
        older = "qq:group:older"
        priority_private = "qq:private:priority"
        coordinator = self._coordinator()
        group_id = "ordered-recovery"
        coordinator.register_group(
            self._group(
                group_id,
                (
                    FocusMember(source, ChatKind.GROUP),
                    FocusMember(newest, ChatKind.GROUP),
                    FocusMember(older, ChatKind.GROUP),
                    FocusMember(priority_private, ChatKind.PRIVATE, display_name="Priority private"),
                ),
                source,
            )
        )
        older_batch = FocusRecoveryBatch("older-batch", older, (21, 22), 100.0, 2)
        newest_batch = FocusRecoveryBatch("newest-batch", newest, (11, 12), 900.0, 1)
        await coordinator.finish_recovery_collection(group_id, (older_batch, newest_batch))

        source_turn = await coordinator.wait_for_turn(source)
        self.assertEqual(source_turn.recovery_batch, newest_batch)
        self.assertEqual(source_turn.recovery_target_chat_id, newest)

        second_source_waiter = asyncio.create_task(coordinator.wait_for_turn(source))
        await asyncio.sleep(0)
        self.assertFalse(second_source_waiter.done(), "Focus issued a second in-flight turn for one group")

        dispatch = await coordinator.route_message(
            _Message(priority_private, "priority private arrived during recovery"),
            StoredMessageRef(1, priority_private, "private-live-1", 1_000.0),
        )
        self.assertTrue(dispatch.managed)
        self.assertTrue(dispatch.woke_active)
        self.assertIsNotNone(dispatch.event)
        self.assertEqual(coordinator._groups[group_id].recovery_queue[0], newest_batch)
        self.assertEqual(coordinator.current_lease(source).chat_id, source)

        switched = await coordinator.switch_to_recovery_head(source_turn.lease, newest_batch)
        self.assertTrue(switched.success, switched.reason)
        self.assertEqual(switched.target_chat_id, newest)
        self.assertFalse(second_source_waiter.done())

        online_turn = await coordinator.wait_for_turn(newest)
        self.assertIsNone(online_turn.recovery_batch)
        self.assertEqual(tuple(event.target_chat_id for event in online_turn.events), (priority_private,))
        self.assertTrue(
            await coordinator.finish_turn(
                online_turn,
                TurnOutcome(TurnStatus.COMPLETED),
            )
        )

        newest_turn = await coordinator.wait_for_turn(newest)
        self.assertEqual(newest_turn.recovery_batch, newest_batch)
        self.assertIsNone(newest_turn.recovery_target_chat_id)
        self.assertTrue(
            await coordinator.finish_turn(
                newest_turn,
                TurnOutcome(TurnStatus.COMPLETED, recovery_delivered=True),
            )
        )

        next_turn = await coordinator.wait_for_turn(newest)
        self.assertEqual(next_turn.recovery_batch, older_batch)
        self.assertEqual(next_turn.recovery_target_chat_id, older)
        switched_again = await coordinator.switch_to_recovery_head(next_turn.lease, older_batch)
        self.assertTrue(switched_again.success, switched_again.reason)
        older_turn = await coordinator.wait_for_turn(older)
        self.assertEqual(older_turn.recovery_batch, older_batch)
        self.assertTrue(
            await coordinator.finish_turn(
                older_turn,
                TurnOutcome(TurnStatus.COMPLETED, recovery_delivered=True),
            )
        )

        state = coordinator._groups[group_id]
        self.assertEqual(state.attention[priority_private].delivered_revision, state.attention[priority_private].revision)
        self.assertFalse(second_source_waiter.done())
        second_source_waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second_source_waiter

    async def test_new_collections_keep_in_flight_head_sort_tail_and_merge_pending_chat(self):
        source = "qq:group:source"
        chat_a = "qq:group:a"
        chat_b = "qq:group:b"
        chat_c = "qq:group:c"
        group_id = "coalesced-recovery-tail"
        coordinator = self._coordinator()
        coordinator.register_group(
            self._group(
                group_id,
                (
                    FocusMember(source, ChatKind.GROUP),
                    FocusMember(chat_a, ChatKind.GROUP),
                    FocusMember(chat_b, ChatKind.GROUP),
                    FocusMember(chat_c, ChatKind.GROUP),
                ),
                source,
            )
        )
        in_flight_batch = FocusRecoveryBatch(
            "in-flight-a", chat_a, (30,), 300.0, 1, row_timestamps=(300.0,)
        )
        pending_b = FocusRecoveryBatch(
            "pending-b", chat_b, (10, 11), 100.0, 2, row_timestamps=(100.0, 125.0)
        )
        await coordinator.finish_recovery_collection(group_id, (pending_b, in_flight_batch))

        source_turn = await coordinator.wait_for_turn(source)
        self.assertEqual(source_turn.recovery_batch, in_flight_batch)
        state = coordinator._groups[group_id]
        second_waiter = asyncio.create_task(coordinator.wait_for_turn(source))
        await asyncio.sleep(0)
        self.assertFalse(second_waiter.done())

        pending_c = FocusRecoveryBatch(
            "pending-c", chat_c, (20,), 200.0, 3, row_timestamps=(200.0,)
        )
        new_b = FocusRecoveryBatch(
            "new-b", chat_b, (1, 12), 50.0, 2, row_timestamps=(50.0, 150.0)
        )
        await coordinator.finish_recovery_collection(group_id, (pending_c, new_b))

        self.assertEqual(state.recovery_queue[0], in_flight_batch)
        self.assertEqual(
            [(batch.chat_id, batch.min_original_timestamp) for batch in state.recovery_queue[1:]],
            [(chat_c, 200.0), (chat_b, 50.0)],
        )
        self.assertEqual(state.recovery_queue[2].row_ids, (1, 10, 11, 12))
        self.assertEqual(state.recovery_queue[2].row_timestamps, (50.0, 100.0, 125.0, 150.0))
        self.assertEqual(
            len({batch.chat_id for batch in state.recovery_queue[1:]}),
            len(state.recovery_queue[1:]),
        )
        self.assertIs(state.in_flight_turn, source_turn)

        switched = await coordinator.switch_to_recovery_head(source_turn.lease, in_flight_batch)
        self.assertTrue(switched.success, switched.reason)
        delivery_turn = await coordinator.wait_for_turn(chat_a)
        self.assertEqual(delivery_turn.recovery_batch, in_flight_batch)
        self.assertTrue(
            await coordinator.finish_turn(
                delivery_turn,
                TurnOutcome(TurnStatus.COMPLETED, recovery_delivered=True),
            )
        )
        self.assertEqual(coordinator.recovered_row_ids_for_chat(chat_a), {30})
        next_turn = await coordinator.wait_for_turn(chat_a)
        self.assertEqual(next_turn.recovery_batch.chat_id, chat_c)
        self.assertFalse(second_waiter.done())
        second_waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second_waiter

    async def test_explicit_recovered_ids_leave_interleaved_live_row_unread_after_receipt_then_cancel(self):
        source = "qq:group:source"
        target = "qq:group:target"
        group_id = "explicit-recovery-ids"
        coordinator = self._coordinator()
        coordinator.register_group(
            self._group(
                group_id,
                (FocusMember(source, ChatKind.GROUP), FocusMember(target, ChatKind.GROUP)),
                source,
            )
        )
        batch = FocusRecoveryBatch("explicit-ids", target, (1, 3), 50.0)
        await coordinator.finish_recovery_collection(group_id, (batch,))

        source_turn = await coordinator.wait_for_turn(source)
        switched = await coordinator.switch_to_recovery_head(source_turn.lease, batch)
        self.assertTrue(switched.success, switched.reason)
        delivery_turn = await coordinator.wait_for_turn(target)
        self.assertEqual(delivery_turn.recovery_batch.row_ids, (1, 3))

        for row_id in (1, 3):
            await coordinator.route_message(
                _Message(target, f"replayed-{row_id}"),
                StoredMessageRef(row_id, target, f"replay-{row_id}", 50.0),
                offline_recovery_row=True,
            )
        live_dispatch = await coordinator.route_message(
            _Message(target, "live row between replay IDs"),
            StoredMessageRef(2, target, "live-2", 100.0),
        )
        self.assertTrue(live_dispatch.woke_active)

        self.assertTrue(
            await coordinator.finish_turn(
                delivery_turn,
                TurnOutcome(TurnStatus.CANCELLED, recovery_delivered=True),
            )
        )

        self.assertEqual(coordinator.recovered_row_ids_for_chat(target), {1, 3})
        self.assertEqual(coordinator.pending_recovery_row_ids_for_chat(target), set())
        following_turn = await coordinator.wait_for_turn(target)
        self.assertIsNone(following_turn.recovery_batch, "a receipted batch was replayed after cancellation")
        self.assertEqual(following_turn.read_after_row_id, 0)
        self.assertEqual(following_turn.read_through_row_id, 3)
        self.assertEqual(following_turn.recovered_row_ids, (1, 3))
        self.assertLess(following_turn.read_after_row_id, 2)
        self.assertLessEqual(2, following_turn.read_through_row_id)
        self.assertNotIn(2, following_turn.recovered_row_ids)
        self.assertTrue(
            await coordinator.finish_turn(
                following_turn,
                TurnOutcome(TurnStatus.COMPLETED, consumed_through_row_id=3),
            )
        )
        self.assertEqual(coordinator._groups[group_id].committed_cursor[target], 3)
        self.assertEqual(coordinator.recovered_row_ids_for_chat(target), set())

    async def test_failed_generation_keeps_head_and_retries_after_retry_deadline(self):
        source = "qq:group:source"
        target = "qq:group:target"
        group_id = "retry-recovery"
        coordinator = self._coordinator()
        coordinator.register_group(
            self._group(
                group_id,
                (FocusMember(source, ChatKind.GROUP), FocusMember(target, ChatKind.GROUP)),
                source,
            )
        )
        batch = FocusRecoveryBatch("retry-head", target, (5, 6), 250.0)
        await coordinator.finish_recovery_collection(group_id, (batch,))
        clock = _Clock()

        with patch.object(coordinator_module, "time", clock):
            failed_turn = await coordinator.wait_for_turn(source)
            failed = await coordinator.finish_turn(
                failed_turn,
                TurnOutcome(TurnStatus.FAILED, recovery_delivered=None),
            )
            self.assertTrue(failed)
            state = coordinator._groups[group_id]
            self.assertEqual(state.recovery_queue[0], batch)
            retry_at = state.recovery_retry_at
            self.assertGreater(retry_at, clock.monotonic())

            retry_waiter = asyncio.create_task(coordinator.wait_for_turn(source))
            await asyncio.sleep(0)
            self.assertFalse(retry_waiter.done(), "failed generation bypassed the retry delay")

            live_dispatch = await coordinator.route_message(
                _Message(source, "fresh live message during recovery backoff"),
                StoredMessageRef(1, source, "live-during-backoff", 300.0),
            )
            self.assertTrue(live_dispatch.woke_active)
            online_turn = await asyncio.wait_for(retry_waiter, timeout=0.5)
            self.assertIsNone(online_turn.recovery_batch)
            self.assertEqual(state.recovery_queue[0], batch)
            self.assertTrue(
                await coordinator.finish_turn(
                    online_turn,
                    TurnOutcome(TurnStatus.COMPLETED, consumed_through_row_id=1),
                )
            )

            retry_waiter = asyncio.create_task(coordinator.wait_for_turn(source))
            await asyncio.sleep(0)
            self.assertFalse(retry_waiter.done(), "online commit cleared the recovery retry delay")

            clock.now = retry_at
            async with state.condition:
                state.condition.notify_all()
            retry_turn = await asyncio.wait_for(retry_waiter, timeout=0.5)
            self.assertEqual(retry_turn.recovery_batch, batch)

    async def test_failed_durable_cas_fences_group_and_leaves_head_unacknowledged(self):
        source = "qq:group:source"
        target = "qq:group:target"
        group_id = "cas-recovery"
        store = _FailingCASStore()
        coordinator = self._coordinator(state_store=store)
        coordinator.register_group(
            self._group(
                group_id,
                (FocusMember(source, ChatKind.GROUP), FocusMember(target, ChatKind.GROUP)),
                source,
            )
        )
        batch = FocusRecoveryBatch("cas-head", target, (1,), 300.0)
        await coordinator.finish_recovery_collection(group_id, (batch,))
        source_turn = await coordinator.wait_for_turn(source)

        result = await coordinator.switch_to_recovery_head(source_turn.lease, batch)
        self.assertFalse(result.success)
        self.assertIn("compare-and-set failed", result.reason)
        self.assertEqual(store.cas_calls, 1)
        self.assertIs(coordinator._groups[group_id].phase, FocusGroupPhase.STOPPED)
        self.assertEqual(coordinator.pending_recovery_row_ids_for_chat(target), {1})

    async def test_policy_denial_is_reported_and_private_source_switch_carries_identity_only_handoff(self):
        source = "qq:group:source"
        private = "qq:private:recovery"
        denied_group = "denied-recovery"
        denied = self._coordinator(policy=ChatScopePolicy(allow_group_to_private=False))
        denied.register_group(
            self._group(
                denied_group,
                (FocusMember(source, ChatKind.GROUP), FocusMember(private, ChatKind.PRIVATE)),
                source,
            )
        )
        denied_batch = FocusRecoveryBatch("denied-head", private, (1,), 300.0)
        await denied.finish_recovery_collection(denied_group, (denied_batch,))
        denied_turn = await denied.wait_for_turn(source)
        denied_result = await denied.switch_to_recovery_head(denied_turn.lease, denied_batch)
        self.assertFalse(denied_result.success)
        self.assertIn("recovery_policy_dropped", denied_result.reason)
        self.assertEqual(denied.pending_recovery_row_ids_for_chat(private), {1})
        self.assertTrue(
            await denied.finish_turn(
                denied_turn,
                TurnOutcome(TurnStatus.NOOP, recovery_dropped=True),
            )
        )
        self.assertEqual(denied.recovered_row_ids_for_chat(private), {1})

        private_source = "qq:private:source"
        group_target = "qq:group:target"
        identity_group = "identity-recovery"
        identity = self._coordinator()
        identity.register_group(
            self._group(
                identity_group,
                (
                    FocusMember(private_source, ChatKind.PRIVATE, display_name="Private Source"),
                    FocusMember(group_target, ChatKind.GROUP, display_name="Group Target"),
                ),
                private_source,
            )
        )
        identity_batch = FocusRecoveryBatch("identity-head", group_target, (8,), 400.0)
        await identity.finish_recovery_collection(identity_group, (identity_batch,))
        private_turn = await identity.wait_for_turn(private_source)
        identity_result = await identity.switch_to_recovery_head(private_turn.lease, identity_batch)
        self.assertTrue(identity_result.success, identity_result.reason)
        self.assertIsNotNone(identity_result.handoff_id)

        handoffs = await identity.handoff_store.get_active(
            identity_group,
            group_target,
            identity_result.new_lease.epoch,
        )
        self.assertEqual(len(handoffs), 1)
        self.assertIs(handoffs[0].kind, HandoffKind.TRANSITION_IDENTITY_V1)
        self.assertTrue(handoffs[0].payload.is_identity_only())
        self.assertEqual(handoffs[0].payload.source_display_name, "Private Source")
        self.assertEqual(handoffs[0].payload.target_display_name, "Group Target")


if __name__ == "__main__":
    unittest.main()
