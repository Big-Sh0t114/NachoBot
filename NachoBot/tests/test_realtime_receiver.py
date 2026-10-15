"""Anonymous realtime ingress must not invoke ordinary-user identity hooks."""
import unittest
from structlog.stdlib import PositionalArgumentsFormatter
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

# Prime Replyer before HeartFlow, matching the existing receiver regressions.
from src.chat.replyer.group_generator import DefaultReplyer  # noqa: F401
from src.chat.heart_flow import heartflow_message_processor as receiver_module
from src.chat.focus.models import StoredMessageRef
from src.chat.realtime_ingress import RealtimeIngressManager, RealtimeProvider


class AnonymousRealtimeReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, verified):
        manager = RealtimeIngressManager()
        manager.register_provider(RealtimeProvider("synthetic", lambda *_: verified))
        runtime = SimpleNamespace(signal_new_message=Mock())
        receiver = object.__new__(receiver_module.HeartFCMessageReceiver)
        receiver.storage = SimpleNamespace(store_message=AsyncMock())
        dispatch = SimpleNamespace(managed=False)
        log = Mock()
        with (
            patch.object(receiver_module, "realtime_ingress_manager", manager),
            patch.object(receiver_module, "_calculate_interest", AsyncMock(return_value=(1.0, []))),
            patch.object(receiver_module.focus_coordinator, "route_message", AsyncMock(return_value=dispatch)) as route,
            patch.object(receiver_module.heartflow, "get_or_create_heartflow_chat", AsyncMock(return_value=runtime)),
            patch.object(receiver_module.offline_recovery_manager, "is_held", return_value=False),
            patch.object(receiver_module.global_config.mood, "enable_mood", True),
            patch.object(receiver_module.mood_manager, "get_mood_by_chat_id", return_value=SimpleNamespace(update_mood_by_message=AsyncMock())) as mood,
            patch.object(receiver_module.Person, "register_person") as person,
            patch.object(receiver_module, "replace_user_references", side_effect=lambda text, *_a, **_kw: text),
            patch.object(receiver_module, "logger", log),
        ):
            for index, label in enumerate(("说话人 A", "说话人 B"), start=1):
                config = {
                    "realtime_session": {"version": 1, "provider": "synthetic", "session_id": "session", "turn_id": str(index)},
                    "runtime_capabilities": {"schema_version": 1, "planner_bypass": True, "reply_delivery": "tts_text", "voice_stream": True},
                    "realtime_speaker": {"version": 1, "speaker_ref": f"speaker-{index}", "label": label},
                }
                group = SimpleNamespace(group_id="group", group_name="Synthetic")
                message = SimpleNamespace(
                    processed_plain_text="recognized speech", is_picid=False, is_emoji=False, is_voice=True,
                    is_mentioned=False, is_at=False, additional_config=config,
                    message_segment=SimpleNamespace(type="voice"),
                    message_info=SimpleNamespace(user_info=None, group_info=group, platform="synthetic", additional_config=config),
                    chat_stream=SimpleNamespace(stream_id="group-chat", group_info=group),
                )
                receiver.storage.store_message.return_value = StoredMessageRef(index, "group-chat", f"voice-{index}", 1.0)
                await receiver.process_message(message)
                self.assertIsNone(message.message_info.user_info)
            self.assertEqual(receiver.storage.store_message.await_count, 2)
            scheduling = [call for call in log.info.call_args_list if call.args and
                          str(call.args[0]).startswith("Realtime ingress scheduling")]
            self.assertEqual(len(scheduling), 4)
            for call in scheduling:
                # Exercise Core's real formatter; a Mock accepts incompatible
                # brace placeholders that abort production message routing.
                event = PositionalArgumentsFormatter()(None, "info", {
                    "event": call.args[0], "positional_args": call.args[1:],
                })
                rendered = event["event"]
                self.assertIn("row_id=", rendered)
                self.assertNotIn("%s", rendered)
                self.assertNotIn("recognized speech", rendered)
                self.assertNotIn("session", rendered)
                self.assertNotIn("speaker-", rendered)
            self.assertEqual(scheduling[0].args[2:4], (verified, verified))
            self.assertEqual(route.await_count, 2)
            self.assertEqual(runtime.signal_new_message.call_count, 2)
            for call in runtime.signal_new_message.call_args_list:
                self.assertEqual(call.kwargs.get("priority_realtime", False), verified)
            self.assertEqual(route.await_args.kwargs["priority_realtime_session"], verified)
            person.assert_not_called()
            mood.assert_not_called()
            log.error.assert_not_called()
            rendered = "\n".join(str(call) for call in log.info.call_args_list)
            for label in ("说话人 A", "说话人 B"):
                self.assertEqual(label in rendered, verified)

    async def test_verified_speakers_are_logged_after_storage_and_wake_without_user_hooks(self):
        await self.exercise(True)

    async def test_unverified_senderless_input_does_not_claim_a_speaker_or_register_person(self):
        await self.exercise(False)
