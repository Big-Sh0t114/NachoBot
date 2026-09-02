"""Regression coverage for private-source Focus routing and switch disposition."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.chat.focus.bypass_gate import FocusBypassDecisionGate, FocusBypassGateError
from src.chat.focus.coordinator import FocusCoordinator
from src.chat.focus.models import (
    ChatKind,
    FocusEventSnapshot,
    FocusGroupDefinition,
    FocusLease,
    FocusMember,
    StaleFocusLeaseError,
    SwitchResult,
    StoredMessageRef,
    TurnOutcome,
    TurnStatus,
)
from src.chat.focus.scope_policy import ChatScopePolicy
from src.chat.focus.switch_action import (
    SwitchDisposition,
    classify_switch_result,
    execute_switch_chat,
    normalize_switch_action_data,
)
from src.config.config import global_config
from src.config.official_configs import FocusConfig, FocusGroupConfig, FocusMemberConfig


def _definition() -> FocusGroupDefinition:
    return FocusGroupDefinition(
        group_id="focus-test",
        members=(
            FocusMember("private-a", ChatKind.PRIVATE, allow_export=False),
            FocusMember("private-b", ChatKind.PRIVATE, allow_export=False),
            FocusMember("group", ChatKind.GROUP),
        ),
        initial_chat_id="private-a",
    )


def _event(target_chat_id: str = "private-b", *, preview: str = "private body") -> FocusEventSnapshot:
    first = StoredMessageRef(1, target_chat_id, "message-1", 1.0)
    return FocusEventSnapshot(
        event_id="event-1",
        revision=1,
        target_chat_id=target_chat_id,
        display_name=target_chat_id,
        unread_count=1,
        first_unread=first,
        last_unread=first,
        is_mentioned=True,
        latest_preview=preview,
    )


class _RecordingHandoffStore:
    async def get_active(self, *_args, **_kwargs):
        raise AssertionError("private-source metadata-only switch must not read handoffs")


class _RecordingCoordinator:
    def __init__(self, policy: ChatScopePolicy, definition: FocusGroupDefinition, event: FocusEventSnapshot):
        self.policy = policy
        self._definition = definition
        self._event = event
        self.handoff_store = _RecordingHandoffStore()
        self.switch_calls: list[tuple[object, object]] = []

    def definition_for_chat(self, chat_id: str):
        return self._definition if chat_id in {member.chat_id for member in self._definition.members} else None

    async def events_for(self, _lease: FocusLease):
        return (self._event,)

    async def switch_chat(self, request, handoff):
        self.switch_calls.append((request, handoff))
        return SwitchResult(
            True,
            "switched",
            request.lease,
            target_chat_id=self._event.target_chat_id,
        )


class _StaleEventsCoordinator(_RecordingCoordinator):
    async def events_for(self, _lease: FocusLease):
        raise StaleFocusLeaseError("Cannot read Focus events with a stale lease")


class FocusPrivateSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_to_private_is_metadata_only_and_does_not_read_source_context(self):
        definition = _definition()
        coordinator = _RecordingCoordinator(ChatScopePolicy(allow_group_to_private=False), definition, _event())
        lease = FocusLease(definition.group_id, "private-a", 1, "turn-1")

        with patch.object(global_config.focus, "mode", "active"):
            with patch("src.chat.focus.switch_action._load_recent_source_results", side_effect=AssertionError):
                with patch("src.chat.focus.switch_action._merge_source_history", side_effect=AssertionError):
                    with patch("src.chat.focus.switch_action.HandoffBuilder.build", side_effect=AssertionError):
                        result = await execute_switch_chat(
                            coordinator,
                            lease=lease,
                            action_data={"event_id": "event-1"},
                        )

        self.assertTrue(result.success)
        self.assertEqual(len(coordinator.switch_calls), 1)
        self.assertIsNone(coordinator.switch_calls[0][1])

    async def test_private_handoff_values_are_rejected_after_normalization(self):
        definition = _definition()
        lease = FocusLease(definition.group_id, "private-a", 1, "turn-1")
        for raw_handoff in ({"task_summary": "secret"}, "malformed", [], {"unknown": []}):
            coordinator = _RecordingCoordinator(ChatScopePolicy(), definition, _event())
            action_data = normalize_switch_action_data({"event_id": "event-1", "handoff": raw_handoff})
            with patch.object(global_config.focus, "mode", "active"):
                result = await execute_switch_chat(
                    coordinator,
                    lease=lease,
                    action_data=action_data,
                )
            self.assertFalse(result.success)
            self.assertIn("private-source metadata-only", result.reason)
            self.assertEqual(coordinator.switch_calls, [])

    async def test_policy_preview_and_config_keep_private_metadata_route(self):
        definition = _definition()
        policy = ChatScopePolicy(allow_group_to_private=False)
        self.assertTrue(policy.can_switch_without_handoff(definition, "private-a", "private-b"))
        self.assertTrue(policy.can_switch_without_handoff(definition, "private-a", "group"))
        self.assertTrue(policy.decide_switch(definition, "private-a", "private-b", has_handoff=False).allowed)
        self.assertFalse(policy.decide_switch(definition, "private-a", "private-b", has_handoff=True).allowed)
        self.assertFalse(policy.can_preview_event(definition, "private-b", "private-a"))
        self.assertFalse(policy.decide_switch(definition, "group", "private-b", has_handoff=False).allowed)

        config_definition = FocusGroupConfig(
            id="focus-test",
            members=[
                FocusMemberConfig(key="a", platform="qq", kind="private", external_id="1"),
                FocusMemberConfig(key="b", platform="qq", kind="private", external_id="2"),
                FocusMemberConfig(key="g", platform="qq", kind="group", external_id="3"),
            ],
            initial_member="a",
        )
        FocusConfig(mode="active", allow_group_to_private=False, groups=[config_definition])

    async def test_private_event_preview_is_suppressed_and_target_turn_has_no_handoff(self):
        definition = _definition()
        coordinator = FocusCoordinator(
            policy=ChatScopePolicy(allow_group_to_private=False),
            unread_event_threshold=5,
        )
        coordinator.register_group(
            definition,
            active_chat_id="private-a",
            epoch=1,
            cursors={member.chat_id: 0 for member in definition.members},
        )
        message = SimpleNamespace(
            chat_stream=SimpleNamespace(stream_id="private-b"),
            processed_plain_text="private body",
            display_message="private body",
            is_mentioned=True,
            is_at=False,
        )
        stored_ref = StoredMessageRef(1, "private-b", "message-1", 1.0)
        dispatch = await coordinator.route_message(message, stored_ref)
        self.assertIsNotNone(dispatch.event)
        self.assertEqual(dispatch.event.latest_preview, "")

        source_turn = await coordinator.wait_for_turn("private-a")
        with patch.object(global_config.focus, "mode", "active"):
            result = await execute_switch_chat(
                coordinator,
                lease=source_turn.lease,
                action_data={"event_id": dispatch.event.event_id},
            )
        self.assertTrue(result.success)
        target_turn = await coordinator.wait_for_turn("private-b")
        self.assertEqual(target_turn.handoff_ids, ())
        self.assertEqual(target_turn.read_after_row_id, 0)
        self.assertEqual(target_turn.read_through_row_id, 1)
        await coordinator.finish_turn(
            target_turn,
            TurnOutcome(status=TurnStatus.COMPLETED, consumed_through_row_id=1),
        )
        await coordinator.stop()

    async def test_failure_disposition_and_runtime_paths_are_centralized(self):
        retry_reasons = (
            "cannot resolve Focus events: timeout",
            "target runtime preparation failed: unavailable",
            "handoff persistence failed: locked",
            "switch persistence failed: locked",
            "switch compare-and-set failed",
            "Focus event revision changed",
            "Focus switch cooldown is active (1.0s remaining)",
        )
        for reason in retry_reasons:
            self.assertIs(
                classify_switch_result(SwitchResult(False, reason, FocusLease("g", "a", 1, "t"))),
                SwitchDisposition.RETRY,
            )
        for reason in (
            "private-source metadata-only switch must not include a handoff",
            "Focus event is no longer pending for this turn",
            "stale source lease",
        ):
            self.assertIs(
                classify_switch_result(SwitchResult(False, reason, FocusLease("g", "a", 1, "t"))),
                SwitchDisposition.DROP,
            )

        definition = _definition()
        lease = FocusLease(definition.group_id, "private-a", 1, "turn-1")
        coordinator = _StaleEventsCoordinator(ChatScopePolicy(), definition, _event())
        with patch.object(global_config.focus, "mode", "active"):
            stale_result = await execute_switch_chat(
                coordinator,
                lease=lease,
                action_data={"event_id": "event-1"},
            )
        self.assertEqual(stale_result.reason.split(":", 1)[0], "stale source lease")
        self.assertIs(classify_switch_result(stale_result), SwitchDisposition.DROP)

        root = Path(__file__).resolve().parents[1]
        brain_source = (root / "src/chat/brain_chat/brain_chat.py").read_text(encoding="utf-8")
        heart_source = (root / "src/chat/heart_flow/heartFC_chat.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(brain_source.count("classify_switch_result"), 2)
        self.assertGreaterEqual(heart_source.count("classify_switch_result"), 4)


class FocusGatePrivateInputTests(unittest.TestCase):
    def test_private_gate_rejects_any_handoff_field(self):
        prompt = FocusBypassDecisionGate._build_prompt(
            (_event(preview="private body"),),
            "",
            event_only=False,
            allow_handoff=False,
        )
        self.assertNotIn("private body", prompt)
        with self.assertRaisesRegex(FocusBypassGateError, "must not include a handoff"):
            FocusBypassDecisionGate._parse_decision(
                '{"decision":"switch","event_id":"event-1","handoff":{}}',
                (_event(),),
                allow_handoff=False,
            )


if __name__ == "__main__":
    unittest.main()
