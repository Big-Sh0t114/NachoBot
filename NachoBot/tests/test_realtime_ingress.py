from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.chat.focus.coordinator import FocusCoordinator
from src.chat.focus.models import (
    ChatKind,
    EffectKind,
    FocusGroupDefinition,
    FocusMember,
    FocusRecoveryBatch,
    StoredMessageRef,
    SwitchChatRequest,
    TurnOutcome,
    TurnStatus,
)
from src.chat.focus.handoff_builder import HandoffBuilder
from src.chat.realtime_ingress import (
    RealtimeIngressDecision,
    RealtimeIngressManager,
    RealtimeLifecycleChange,
    RealtimeProvider,
)


_CHAT_ID = "synthetic-private-7"
_SESSION_ID = "opaque-session-7"


def _message(
    *,
    turn_id: str = "turn-1",
    row_id: int = 200,
    session_id: str = _SESSION_ID,
    chat_id: str = _CHAT_ID,
):
    config = {
        "realtime_session": {
            "version": 1,
            "provider": "synthetic",
            "session_id": session_id,
            "turn_id": turn_id,
        },
        "runtime_capabilities": {
            "schema_version": 1,
            "planner_bypass": True,
            "reply_delivery": "tts_text",
            "voice_stream": True,
        },
    }
    user = SimpleNamespace(user_id="7", user_nickname="Speaker")
    return SimpleNamespace(
        message_id=f"synthetic:turn:{turn_id}",
        focus_row_id=row_id,
        time=100.0,
        processed_plain_text=f"speech {turn_id}",
        user_info=user,
        message_info=SimpleNamespace(
            platform="synthetic",
            message_id=f"synthetic:turn:{turn_id}",
            user_info=user,
            group_info=None,
            additional_config=config,
        ),
        additional_config=config,
        group_info=None,
        chat_info_group_id=None,
        chat_info_stream_id=chat_id,
        chat_info_platform="synthetic",
    )


def _provider(verify=None, lifecycle=None, enabled=lambda: True):
    return RealtimeProvider(
        "synthetic",
        verify or (lambda _message, _chat, session: session.session_id == _SESSION_ID),
        lifecycle=lifecycle,
        enabled=enabled,
    )


async def _authorize_and_record(manager: RealtimeIngressManager, message, row_id: int) -> RealtimeIngressDecision:
    decision = await manager.inspect_message(message, _CHAT_ID)
    stored = StoredMessageRef(row_id, _CHAT_ID, message.message_id, message.time)
    manager.record_message(decision, stored)
    return decision


class RealtimeIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_provider_authorizes_rows_and_replays_stay_excluded(self):
        manager = RealtimeIngressManager()
        manager.register_provider(_provider())
        ordinary = SimpleNamespace(message_id="ordinary", additional_config={})
        invalid = _message(session_id="other-session")
        unknown_config = {
            "realtime_session": {
                "version": 1,
                "provider": "unregistered",
                "session_id": "opaque-session-7",
                "turn_id": "turn-unknown",
            }
        }
        unknown = SimpleNamespace(message_id="unknown", additional_config=unknown_config)

        self.assertFalse((await manager.inspect_message(ordinary, _CHAT_ID)).declared)
        invalid_decision = await manager.inspect_message(invalid, _CHAT_ID)
        self.assertTrue(invalid_decision.declared)
        self.assertFalse(invalid_decision.authorized)
        unknown_decision = await manager.inspect_message(unknown, _CHAT_ID)
        self.assertTrue(unknown_decision.declared)
        self.assertFalse(unknown_decision.authorized)
        self.assertEqual(manager.filter_ordinary_messages(_CHAT_ID, [ordinary, invalid, unknown]), [ordinary])

        voice = _message()
        decision = await _authorize_and_record(manager, voice, 200)
        self.assertTrue(decision.authorized)
        self.assertEqual(manager.pending_row_ids(_CHAT_ID), (200,))
        self.assertEqual(manager.filter_ordinary_messages(_CHAT_ID, [voice, ordinary]), [ordinary])
        self.assertTrue(await manager.revalidate_message(voice, _CHAT_ID))
        manager.complete_rows(_CHAT_ID, (200,))
        self.assertEqual(manager.pending_row_ids(_CHAT_ID), ())
        self.assertIn(200, manager.excluded_row_ids_for_chat(_CHAT_ID))

        replay = await manager.inspect_message(voice, _CHAT_ID)
        self.assertFalse(replay.authorized)
        self.assertFalse(manager.record_message(replay, StoredMessageRef(201, _CHAT_ID, voice.message_id, 100.0)))
        self.assertEqual(manager.pending_row_ids(_CHAT_ID), ())

    async def test_unverified_new_turn_does_not_retire_the_current_authorized_row(self):
        manager = RealtimeIngressManager()
        verifier = AsyncMock(side_effect=lambda _message, _chat, session: session.turn_id == "turn-1")
        manager.register_provider(_provider(verifier))
        current = _message(turn_id="turn-1", row_id=200)
        await _authorize_and_record(manager, current, 200)
        self.assertEqual(manager.pending_row_ids(_CHAT_ID), (200,))

        invalid_new_turn = _message(turn_id="turn-invalid", row_id=201)
        invalid = await manager.inspect_message(invalid_new_turn, _CHAT_ID)
        self.assertFalse(invalid.authorized)
        self.assertEqual(manager.pending_row_ids(_CHAT_ID), (200,))
        self.assertTrue(manager.has_active_session(_CHAT_ID))
        self.assertEqual(
            manager.filter_ordinary_messages(_CHAT_ID, [current, invalid_new_turn]),
            [],
        )

    async def test_older_slow_proof_cannot_commit_after_a_newer_verified_turn(self):
        manager = RealtimeIngressManager()
        release = {"turn-old": asyncio.Event(), "turn-new": asyncio.Event()}

        async def verifier(_message, _chat, session):
            await release[session.turn_id].wait()
            return True

        manager.register_provider(_provider(verifier))
        old_message = _message(turn_id="turn-old", row_id=200)
        new_message = _message(turn_id="turn-new", row_id=201)
        old_task = asyncio.create_task(manager.inspect_message(old_message, _CHAT_ID))
        await asyncio.sleep(0)
        new_task = asyncio.create_task(manager.inspect_message(new_message, _CHAT_ID))
        await asyncio.sleep(0)
        release["turn-new"].set()
        new_decision = await new_task
        self.assertTrue(new_decision.authorized)
        self.assertTrue(manager.record_message(new_decision, StoredMessageRef(201, _CHAT_ID, new_message.message_id, 100.0)))
        release["turn-old"].set()
        old_decision = await old_task
        self.assertFalse(old_decision.authorized)
        self.assertFalse(manager.record_message(old_decision, StoredMessageRef(200, _CHAT_ID, old_message.message_id, 100.0)))
        self.assertEqual(manager.pending_row_ids(_CHAT_ID), (201,))

    async def test_terminal_lifecycle_revokes_slow_status_proof(self):
        manager = RealtimeIngressManager()
        release_verifier = asyncio.Event()

        async def verifier(_message, _chat, _session):
            await release_verifier.wait()
            return True

        def lifecycle(message, chat_id):
            if getattr(message, "terminal", False):
                return RealtimeLifecycleChange("synthetic", _SESSION_ID, chat_id, False)
            return None

        manager.register_provider(_provider(verifier, lifecycle=lifecycle))
        message = _message()
        proof_task = asyncio.create_task(manager.inspect_message(message, _CHAT_ID))
        await asyncio.sleep(0)
        terminal = SimpleNamespace(terminal=True, additional_config={})
        await manager.inspect_message(terminal, _CHAT_ID)
        release_verifier.set()
        decision = await proof_task
        self.assertFalse(decision.authorized)
        self.assertFalse(manager.has_active_session(_CHAT_ID))
        self.assertEqual(manager.pending_row_ids(_CHAT_ID), ())

    async def test_provider_unregister_fences_slow_status_proof(self):
        manager = RealtimeIngressManager()
        release_verifier = asyncio.Event()

        async def verifier(_message, _chat, _session):
            await release_verifier.wait()
            return True

        manager.register_provider(_provider(verifier))
        task = asyncio.create_task(manager.inspect_message(_message(), _CHAT_ID))
        await asyncio.sleep(0)
        manager.unregister_provider("synthetic")
        release_verifier.set()
        decision = await task
        self.assertFalse(decision.authorized)
        self.assertFalse(manager.has_active_session(_CHAT_ID))

    async def test_expired_session_retires_pending_rows(self):
        manager = RealtimeIngressManager()
        manager.session_ttl_seconds = 0.02
        manager.register_provider(_provider())
        voice = _message()
        await _authorize_and_record(manager, voice, 200)
        self.assertEqual(manager.pending_row_ids(_CHAT_ID), (200,))
        await asyncio.sleep(0.03)
        self.assertEqual(manager.pending_row_ids(_CHAT_ID), ())
        self.assertFalse(manager.has_active_session(_CHAT_ID))
        self.assertIn(200, manager.excluded_row_ids_for_chat(_CHAT_ID))

    async def test_focus_realtime_turn_keeps_zero_cursor_and_does_not_ack_recovery(self):
        manager = RealtimeIngressManager()
        manager.register_provider(_provider())
        coordinator = FocusCoordinator()
        manager_patch = patch("src.chat.realtime_ingress.realtime_ingress_manager", manager)
        coordinator_patch = patch("src.chat.focus.coordinator.focus_coordinator", coordinator)
        manager_patch.start()
        coordinator_patch.start()
        self.addCleanup(manager_patch.stop)
        self.addCleanup(coordinator_patch.stop)
        coordinator.register_group(
            FocusGroupDefinition(
                group_id="realtime-priority",
                members=(FocusMember(_CHAT_ID, ChatKind.PRIVATE), FocusMember("other-private", ChatKind.PRIVATE)),
                initial_chat_id=_CHAT_ID,
            )
        )
        batch = FocusRecoveryBatch("history-1", _CHAT_ID, (17, 18), 50.0)
        await coordinator.finish_recovery_collection("realtime-priority", (batch,))
        history_turn = await coordinator.wait_for_turn(_CHAT_ID)
        self.assertIs(history_turn.recovery_batch, batch)

        voice = _message()
        decision = await manager.inspect_message(voice, _CHAT_ID)
        stored = StoredMessageRef(200, _CHAT_ID, voice.message_id, voice.time)
        self.assertTrue(manager.record_message(decision, stored))
        dispatch = await coordinator.route_message(
            voice,
            stored,
            priority_realtime_session=True,
        )
        self.assertTrue(dispatch.woke_active)
        self.assertTrue(dispatch.interrupt_active)
        state = coordinator._groups["realtime-priority"]
        self.assertEqual(state.latest_live_message_row.get(_CHAT_ID, 0), 0)

        await coordinator.finish_turn(
            history_turn,
            TurnOutcome(TurnStatus.CANCELLED, consumed_through_row_id=0),
        )
        realtime_turn = await coordinator.wait_for_turn(_CHAT_ID)
        self.assertIsNone(realtime_turn.recovery_batch)
        self.assertEqual(realtime_turn.read_after_row_id, realtime_turn.read_through_row_id)
        self.assertEqual(realtime_turn.read_through_row_id, 0)
        async with coordinator.effect_permit(
            realtime_turn.lease,
            EffectKind.SEND,
            target_chat_id=_CHAT_ID,
        ) as permit:
            self.assertTrue(permit.managed)
        manager.complete_rows(_CHAT_ID, (200,))
        await coordinator.finish_turn(
            realtime_turn,
            TurnOutcome(TurnStatus.COMPLETED, consumed_through_row_id=0),
        )
        self.assertEqual(state.committed_cursor[_CHAT_ID], 0)
        self.assertEqual(state.recovery_queue, [batch])

    async def test_nonactive_member_realtime_session_routes_through_focus_handoff(self):
        active_chat = "synthetic-private-active"
        target_chat = "synthetic-private-caller"
        manager = RealtimeIngressManager()
        manager.register_provider(_provider())
        coordinator = FocusCoordinator()
        manager_patch = patch("src.chat.realtime_ingress.realtime_ingress_manager", manager)
        coordinator_patch = patch("src.chat.focus.coordinator.focus_coordinator", coordinator)
        manager_patch.start()
        coordinator_patch.start()
        self.addCleanup(manager_patch.stop)
        self.addCleanup(coordinator_patch.stop)
        coordinator.register_group(
            FocusGroupDefinition(
                group_id="synthetic-nonactive-realtime",
                members=(
                    FocusMember(active_chat, ChatKind.PRIVATE),
                    FocusMember(target_chat, ChatKind.PRIVATE),
                ),
                initial_chat_id=active_chat,
            )
        )

        voice = _message(chat_id=target_chat)
        stored = StoredMessageRef(200, target_chat, voice.message_id, voice.time)
        decision = await manager.inspect_message(voice, target_chat)
        self.assertTrue(decision.authorized)
        self.assertTrue(manager.record_message(decision, stored))
        dispatch = await coordinator.route_message(
            voice,
            stored,
            priority_realtime_session=True,
        )
        self.assertTrue(dispatch.woke_active)
        self.assertEqual(dispatch.active_chat_id, active_chat)
        self.assertEqual(manager.pending_row_ids(target_chat), (200,))
        self.assertEqual(coordinator.pending_runtime_chat_ids(), (active_chat,))

        source_turn = await coordinator.wait_for_turn(active_chat)
        self.assertEqual(len(source_turn.events), 1)
        self.assertEqual(source_turn.events[0].target_chat_id, target_chat)
        self.assertEqual(source_turn.events[0].latest_preview, "")
        definition = coordinator.definition_for_chat(active_chat)
        handoff = HandoffBuilder().build_transition_identity(
            definition=definition,
            group_id="synthetic-nonactive-realtime",
            source_chat_id=active_chat,
            target_chat_id=target_chat,
            source_epoch=source_turn.lease.epoch,
            policy_version=coordinator.policy.version,
        )
        switched = await coordinator.switch_chat(
            SwitchChatRequest(
                lease=source_turn.lease,
                event_id=source_turn.events[0].event_id,
                expected_event_revision=source_turn.events[0].revision,
            ),
            handoff,
        )
        self.assertTrue(switched.success, switched.reason)
        self.assertEqual(switched.target_chat_id, target_chat)

        realtime_turn = await coordinator.wait_for_turn(target_chat)
        self.assertIsNone(realtime_turn.recovery_batch)
        self.assertEqual(realtime_turn.read_after_row_id, realtime_turn.read_through_row_id)
        self.assertEqual(realtime_turn.read_through_row_id, 0)
        async with coordinator.effect_permit(
            realtime_turn.lease,
            EffectKind.SEND,
            target_chat_id=target_chat,
        ) as permit:
            self.assertTrue(permit.managed)
            self.assertEqual(permit.target_chat_id, target_chat)
        manager.complete_rows(target_chat, (200,))
        await coordinator.finish_turn(
            realtime_turn,
            TurnOutcome(TurnStatus.COMPLETED, consumed_through_row_id=0),
        )

    async def test_expired_realtime_lease_wakes_queued_recovery_without_terminal_event(self):
        manager = RealtimeIngressManager()
        manager.session_ttl_seconds = 0.04
        lifecycle = lambda message, chat_id: (
            RealtimeLifecycleChange("synthetic", _SESSION_ID, chat_id, True, 0.04)
            if getattr(message, "open_lease", False)
            else None
        )
        manager.register_provider(_provider(lifecycle=lifecycle))
        coordinator = FocusCoordinator()
        manager_patch = patch("src.chat.realtime_ingress.realtime_ingress_manager", manager)
        coordinator_patch = patch("src.chat.focus.coordinator.focus_coordinator", coordinator)
        manager_patch.start()
        coordinator_patch.start()
        self.addCleanup(manager_patch.stop)
        self.addCleanup(coordinator_patch.stop)
        coordinator.register_group(
            FocusGroupDefinition(
                group_id="synthetic-realtime-expiry",
                members=(FocusMember(_CHAT_ID, ChatKind.PRIVATE), FocusMember("other-private", ChatKind.PRIVATE)),
                initial_chat_id=_CHAT_ID,
            )
        )
        batch = FocusRecoveryBatch("history-expiry", _CHAT_ID, (17,), 50.0)
        await coordinator.finish_recovery_collection("synthetic-realtime-expiry", (batch,))

        decision = await manager.inspect_message(SimpleNamespace(open_lease=True), _CHAT_ID)
        self.assertFalse(decision.declared)
        self.assertTrue(manager.has_active_session(_CHAT_ID))
        turn = await asyncio.wait_for(coordinator.wait_for_turn(_CHAT_ID), timeout=0.5)
        self.assertIs(turn.recovery_batch, batch)
        self.assertFalse(manager.has_active_session(_CHAT_ID))

    async def test_claimed_recovery_turn_never_runs_pending_realtime_rows(self):
        for module_name in (
            "src.chat.brain_chat.brain_chat",
            "src.chat.heart_flow.heartFC_chat",
        ):
            module = __import__(module_name, fromlist=["realtime_ingress_manager"])
            runtime_type = module.BrainChatting if module_name.endswith("brain_chat") else module.HeartFChatting
            manager = RealtimeIngressManager()
            manager.register_provider(_provider())
            voice = _message()
            await _authorize_and_record(manager, voice, 200)

            runtime = object.__new__(runtime_type)
            runtime.stream_id = _CHAT_ID
            runtime._offline_recovery_batch = object()
            runtime._offline_recovery_delivered = False
            runtime._focus_turn_interrupted = False
            runtime._ensure_offline_recovery_state = Mock()
            runtime._observe = AsyncMock(return_value=True)
            runtime.blocked_users = {}
            runtime._filter_blocked_users = Mock(side_effect=lambda rows, **_kwargs: rows)
            loader = Mock(return_value=[voice])
            with (
                self.subTest(runtime=runtime_type.__name__),
                patch.object(module, "realtime_ingress_manager", manager),
                patch.object(module, "load_message_rows_by_ids", loader),
            ):
                result = await runtime._loopbody(focus_turn=SimpleNamespace(recovery_batch=object()))

            self.assertFalse(result)
            self.assertTrue(runtime._focus_turn_interrupted)
            self.assertFalse(runtime._offline_recovery_delivered)
            runtime._observe.assert_not_awaited()
            loader.assert_not_called()
            self.assertEqual(manager.pending_row_ids(_CHAT_ID), (200,))

    async def test_real_time_rows_are_loaded_by_id_and_bypass_ordinary_cursor(self):
        manager = RealtimeIngressManager()
        manager.register_provider(_provider())
        voice = _message()
        decision = await manager.inspect_message(voice, _CHAT_ID)
        manager.record_message(decision, StoredMessageRef(200, _CHAT_ID, voice.message_id, voice.time))

        for module_name in (
            "src.chat.brain_chat.brain_chat",
            "src.chat.heart_flow.heartFC_chat",
        ):
            manager = RealtimeIngressManager()
            manager.register_provider(_provider())
            decision = await manager.inspect_message(voice, _CHAT_ID)
            manager.record_message(decision, StoredMessageRef(200, _CHAT_ID, voice.message_id, voice.time))
            module = __import__(module_name, fromlist=["offline_recovery_manager"])
            runtime_type = module.BrainChatting if module_name.endswith("brain_chat") else module.HeartFChatting
            runtime = object.__new__(runtime_type)
            runtime.stream_id = _CHAT_ID
            runtime.last_read_time = 0.0
            runtime._offline_recovery_batch = None
            runtime._ensure_offline_recovery_state = Mock()
            runtime._observe = AsyncMock(return_value=True)
            runtime.blocked_users = {}
            runtime._filter_blocked_users = Mock(side_effect=lambda rows, **_kwargs: rows)
            loader = Mock(return_value=[voice])
            with (
                patch.object(module, "realtime_ingress_manager", manager),
                patch.object(module, "load_message_rows_by_ids", loader),
            ):
                await runtime._loopbody()
            loader.assert_called_once_with(_CHAT_ID, (200,))
            runtime._observe.assert_awaited_once()
            self.assertEqual(runtime.last_read_time, 0.0)
            self.assertEqual(manager.pending_row_ids(_CHAT_ID), ())

    async def test_recovery_handoff_is_refused_while_realtime_rows_wait(self):
        manager = RealtimeIngressManager()
        manager.register_provider(_provider())
        coordinator = FocusCoordinator()
        coordinator.register_group(
            FocusGroupDefinition(
                group_id="realtime-handoff",
                members=(FocusMember(_CHAT_ID, ChatKind.PRIVATE), FocusMember("other-private", ChatKind.PRIVATE)),
                initial_chat_id=_CHAT_ID,
            )
        )
        batch = FocusRecoveryBatch("history-other", "other-private", (30,), 40.0)
        await coordinator.finish_recovery_collection("realtime-handoff", (batch,))
        turn = await coordinator.wait_for_turn(_CHAT_ID)
        voice = _message()
        decision = await manager.inspect_message(voice, _CHAT_ID)
        manager.record_message(decision, StoredMessageRef(300, _CHAT_ID, voice.message_id, voice.time))
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", manager):
            result = await coordinator.switch_to_recovery_head(turn.lease, batch)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "realtime_priority_preempts_recovery")
        state = coordinator._groups["realtime-handoff"]
        self.assertEqual(state.active_chat_id, _CHAT_ID)
        self.assertEqual(state.recovery_queue, [batch])
        await coordinator.finish_turn(turn, TurnOutcome(TurnStatus.FAILED, consumed_through_row_id=0))

    def test_brain_and_replyer_modules_import_in_fresh_processes(self):
        project_root = Path(__file__).resolve().parents[1]
        probes = (
            (
                "brain_chat",
                "import src.chat.brain_chat.brain_chat; print('IMPORT_OK')",
            ),
            (
                "public generator_api (DefaultReplyer + PrivateReplyer)",
                "from src.plugin_system.apis import generator_api; "
                "assert generator_api.DefaultReplyer and generator_api.PrivateReplyer; "
                "print('IMPORT_OK')",
            ),
        )
        with tempfile.TemporaryDirectory(prefix="nachobot-import-cwd-") as import_cwd:
            for probe_name, command in probes:
                with self.subTest(module=probe_name):
                    isolated_command = (
                        f"import sys; sys.path.insert(0, {str(project_root)!r}); {command}"
                    )
                    result = subprocess.run(
                        [sys.executable, "-c", isolated_command],
                        cwd=import_cwd,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        msg=(result.stdout + "\n" + result.stderr)[-6000:],
                    )
                    self.assertIn("IMPORT_OK", result.stdout)

    async def test_planner_and_replyers_share_retryable_memory_prompt_initialization(self):
        # Replyers are exported through the public generator API. Directly
        # importing either implementation as the first module reproduces an
        # unrelated baseline plugin-package cycle.
        importlib.import_module("src.plugin_system.apis.generator_api")
        group_module = sys.modules["src.chat.replyer.group_generator"]
        private_module = sys.modules["src.chat.replyer.private_generator"]
        memory_module = importlib.import_module("src.memory_system.memory_retrieval")
        tool_use_module = importlib.import_module("src.plugin_system.core.tool_use")
        mcp_executor_module = importlib.import_module("src.plugin_system.core.mcp_tool_executor")
        local_store_module = importlib.import_module("src.manager.local_store_manager")

        init_calls = []
        fake_model_config = SimpleNamespace(
            model_task_config=SimpleNamespace(tool_use=object(), mcp=SimpleNamespace(model_list=[]))
        )
        fake_chat_stream = SimpleNamespace(stream_id="synthetic-replyer", group_info=None, user_info=None)

        original_shared_initialized = memory_module._memory_retrieval_prompt_initialized
        original_group_initialized = group_module._memory_retrieval_prompt_initialized
        original_private_initialized = private_module._memory_retrieval_prompt_initialized
        memory_module._memory_retrieval_prompt_initialized = False
        group_module._memory_retrieval_prompt_initialized = False
        private_module._memory_retrieval_prompt_initialized = False
        try:
            with patch.object(
                memory_module,
                "runtime_capabilities_from_stream",
                return_value=SimpleNamespace(memory_retrieval=False),
            ), patch.object(memory_module, "init_memory_retrieval_prompt", side_effect=lambda: init_calls.append("init")):
                # BrainPlanner calls this builder while assembling its prompt,
                # before an action can construct either Replyer.
                self.assertEqual(
                    await memory_module.build_memory_retrieval_prompt(
                        message="planner", sender="sender", target="target", chat_stream=fake_chat_stream
                    ),
                    "",
                )
                self.assertEqual(init_calls, ["init"])
                self.assertTrue(memory_module._memory_retrieval_prompt_initialized)

                with ExitStack() as stack:
                    for replyer_module in (group_module, private_module):
                        stack.enter_context(
                            patch.object(replyer_module, "get_chat_type_and_target_info", return_value=(False, "target"))
                        )
                        stack.enter_context(patch.object(replyer_module, "UniversalMessageSender", return_value=object()))
                        stack.enter_context(patch.object(replyer_module, "WebSearchManager", return_value=object()))
                        stack.enter_context(patch.object(replyer_module, "CapabilityRouter", return_value=object()))
                        stack.enter_context(patch.object(replyer_module, "UrlContentFetcher", return_value=object()))
                        stack.enter_context(patch.object(replyer_module, "model_config", fake_model_config))
                    stack.enter_context(patch.object(tool_use_module, "ToolExecutor", return_value=object()))
                    stack.enter_context(patch.object(mcp_executor_module, "MCPToolExecutor", return_value=object()))
                    stack.enter_context(patch.object(local_store_module, "local_storage", defaultdict(lambda: None)))

                    group_module.DefaultReplyer(fake_chat_stream)
                    group_module.DefaultReplyer(fake_chat_stream)
                    private_module.PrivateReplyer(fake_chat_stream)
                    private_module.PrivateReplyer(fake_chat_stream)
                    self.assertEqual(init_calls, ["init"])

                    self.assertEqual(
                        await group_module.build_memory_retrieval_prompt(
                            message="group", sender="sender", target="target", chat_stream=fake_chat_stream
                        ),
                        "",
                    )
                    self.assertEqual(
                        await private_module.build_memory_retrieval_prompt(
                            message="private", sender="sender", target="target", chat_stream=fake_chat_stream
                        ),
                        "",
                    )
                    self.assertEqual(init_calls, ["init"])

                    # A Replyer can be the first consumer too: repeated
                    # instances in either module share the same initializer.
                    memory_module._memory_retrieval_prompt_initialized = False
                    group_module._memory_retrieval_prompt_initialized = False
                    private_module._memory_retrieval_prompt_initialized = False
                    init_calls.clear()
                    group_module.DefaultReplyer(fake_chat_stream)
                    group_module.DefaultReplyer(fake_chat_stream)
                    self.assertEqual(init_calls, ["init"])
                    private_module.PrivateReplyer(fake_chat_stream)
                    private_module.PrivateReplyer(fake_chat_stream)
                    self.assertEqual(init_calls, ["init"])

            memory_module._memory_retrieval_prompt_initialized = False
            group_module._memory_retrieval_prompt_initialized = False
            private_module._memory_retrieval_prompt_initialized = False
            failure_calls = []

            def fail_once_then_initialize():
                failure_calls.append("init")
                if len(failure_calls) == 1:
                    raise RuntimeError("prompt registration failure")

            with patch.object(memory_module, "init_memory_retrieval_prompt", side_effect=fail_once_then_initialize):
                with patch.object(
                    memory_module,
                    "runtime_capabilities_from_stream",
                    return_value=SimpleNamespace(memory_retrieval=False),
                ):
                    with self.assertRaisesRegex(RuntimeError, "prompt registration failure"):
                        await memory_module.build_memory_retrieval_prompt(
                            message="planner", sender="sender", target="target", chat_stream=fake_chat_stream
                        )
                    self.assertFalse(memory_module._memory_retrieval_prompt_initialized)

                    self.assertEqual(
                        await group_module.build_memory_retrieval_prompt(
                            message="group", sender="sender", target="target", chat_stream=fake_chat_stream
                        ),
                        "",
                    )
                    self.assertTrue(memory_module._memory_retrieval_prompt_initialized)
                    self.assertTrue(group_module._memory_retrieval_prompt_initialized)
                    private_module._ensure_memory_retrieval_prompt_initialized()
                    self.assertTrue(private_module._memory_retrieval_prompt_initialized)
                    group_module._ensure_memory_retrieval_prompt_initialized()
                    self.assertEqual(len(failure_calls), 2)
        finally:
            memory_module._memory_retrieval_prompt_initialized = original_shared_initialized
            group_module._memory_retrieval_prompt_initialized = original_group_initialized
            private_module._memory_retrieval_prompt_initialized = original_private_initialized

    def test_memory_prompt_ensure_serializes_concurrent_first_use(self):
        memory_module = importlib.import_module("src.memory_system.memory_retrieval")
        original_initialized = memory_module._memory_retrieval_prompt_initialized
        memory_module._memory_retrieval_prompt_initialized = False
        calls = []
        start_together = threading.Barrier(8)

        def initialize_once():
            calls.append("init")
            time.sleep(0.02)

        def ensure_from_worker(_index):
            start_together.wait(timeout=5)
            memory_module.ensure_memory_retrieval_prompt_initialized()

        try:
            with patch.object(memory_module, "init_memory_retrieval_prompt", side_effect=initialize_once):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(ensure_from_worker, range(8)))
            self.assertEqual(calls, ["init"])
            self.assertTrue(memory_module._memory_retrieval_prompt_initialized)
        finally:
            memory_module._memory_retrieval_prompt_initialized = original_initialized


if __name__ == "__main__":
    unittest.main()
