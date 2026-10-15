"""A verified background group call must not wait for a Planner/Gate vote."""
import unittest
from unittest.mock import patch

from test_realtime_ingress import _message, _provider
from src.chat.replyer.group_generator import DefaultReplyer  # noqa: F401 - initialize replyer imports
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.chat.focus.coordinator import FocusCoordinator
from src.chat.focus.models import ChatKind, FocusGroupDefinition, FocusMember, StoredMessageRef
from src.chat.realtime_ingress import RealtimeIngressManager


class RealtimeFocusPreemptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = RealtimeIngressManager()
        self.manager.register_provider(_provider())
        self.coordinator = FocusCoordinator(unread_event_threshold=99)
        self.coordinator.register_group(FocusGroupDefinition(
            group_id="groups", initial_chat_id="source",
            members=(FocusMember("source", ChatKind.GROUP), FocusMember("target", ChatKind.GROUP)),
        ))
        self.patch_manager = patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager)
        self.patch_manager.start()
        self.addCleanup(self.patch_manager.stop)
        self.patch_coordinator = patch("src.chat.focus.coordinator.focus_coordinator", self.coordinator)
        self.patch_coordinator.start()
        self.addCleanup(self.patch_coordinator.stop)

    async def route(self, *, authorized=True):
        voice = _message(chat_id="target", session_id="opaque-session-7" if authorized else "forged")
        stored = StoredMessageRef(200, "target", voice.message_id, voice.time)
        decision = await self.manager.inspect_message(voice, "target")
        recorded = self.manager.record_message(decision, stored)
        return await self.coordinator.route_message(voice, stored, priority_realtime_session=recorded)

    async def test_equal_priority_group_call_gets_deterministic_hfc_switch(self):
        dispatch = await self.route()
        self.assertTrue(dispatch.interrupt_active)
        self.assertTrue(self.coordinator.is_forced_ingress_preemption("source", "target"))
        turn = await self.coordinator.wait_for_turn("source")
        self.assertEqual(turn.read_after_row_id, turn.read_through_row_id)
        action = HeartFChatting._focus_forced_priority_action(self.coordinator, "source", turn, {})
        self.assertIsNotNone(action, "verified group call fell through to the Gate")
        self.assertEqual(action.action_type, "switch_chat")
        self.assertEqual(action.action_data["event_id"], turn.events[0].event_id)
        self.assertIn("handoff", action.action_data, "preserve ordinary cross-group handoff")

    async def test_completed_rows_do_not_keep_forcing_switch(self):
        await self.route()
        turn = await self.coordinator.wait_for_turn("source")
        self.manager.complete_rows("target", (200,))
        self.assertFalse(self.coordinator.has_pending_realtime_ingress("target"))
        self.assertFalse(self.coordinator.is_forced_ingress_preemption("source", "target"))
        self.assertIsNone(HeartFChatting._focus_forced_priority_action(self.coordinator, "source", turn, {}))

    async def test_expired_provider_proof_does_not_keep_forcing_switch(self):
        await self.route()
        turn = await self.coordinator.wait_for_turn("source")
        for lease in self.manager._sessions.values():
            lease.expires_at = 0
        self.assertFalse(self.coordinator.is_forced_ingress_preemption("source", "target"))
        self.assertIsNone(HeartFChatting._focus_forced_priority_action(self.coordinator, "source", turn, {}))

    async def test_unverified_declaration_does_not_force_switch(self):
        dispatch = await self.route(authorized=False)
        self.assertFalse(dispatch.woke_active)
        self.assertFalse(dispatch.interrupt_active)
        self.assertFalse(self.coordinator.is_forced_ingress_preemption("source", "target"))

    async def test_realtime_ingress_preserves_scope_policy(self):
        await self.route()
        turn = await self.coordinator.wait_for_turn("source")
        with patch.object(self.coordinator.policy, "can_emit_event", return_value=False):
            self.assertFalse(self.coordinator.is_forced_ingress_preemption("source", "target"))
            self.assertIsNone(HeartFChatting._focus_forced_priority_action(self.coordinator, "source", turn, {}))


if __name__ == "__main__":
    unittest.main()
