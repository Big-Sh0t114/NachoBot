from __future__ import annotations

import asyncio
import base64
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QQCALL_ROOT = PROJECT_ROOT.parent / "NachoBot-QQCall"
if str(QQCALL_ROOT) not in sys.path:
    sys.path.insert(0, str(QQCALL_ROOT))

from ncnk_message import BaseMessageInfo, GroupInfo, MessageBase, Seg
from qqcall_client.qqcall_group_message import build_qqcall_group_voice_message
from plugins.qqcall_plugin import group_realtime_provider
from plugins.qqcall_plugin import plugin as qqcall_plugin
from plugins.qqcall_plugin.realtime_provider import verify_qqcall_realtime_message
from qqcall_pcm.group_protocol import anonymous_speaker_annotation
from src.chat.focus.models import StoredMessageRef
from src.chat.realtime_ingress import RealtimeIngressManager, RealtimeProvider
from src.chat.runtime_capabilities import classify_runtime_message_batch, runtime_capabilities_from_message
from src.common.data_models.database_data_model import DatabaseMessages
from plugins.qqcall_plugin.control_api import QQCallAPIError
from src.plugin_system.apis.realtime_api import RealtimeSession


GROUP_ID = "123456789"
REQUESTER = "53400"
CHAT_ID = "qq-group-stream-123456789"
SCOPE = {
    "kind": "qqcall_group",
    "serviceInstance": "service-7",
    "hostGeneration": 159,
    "connectionGeneration": 161,
    "groupId": GROUP_ID,
    "roomId": "room-44",
    "roomGeneration": 3,
}
UVC_SESSION = {
    "service_instance_id": "uvc-service-1",
    "session_id": "uvc-session-1",
    "session_generation": "uvc-generation-1",
    "source_id": "main",
}
VOICE_BASE64 = base64.b64encode(b"RIFF\x00\x00\x00\x00WAVEvoice-fixture").decode("ascii")


def _envelope(*, turn_id: str = "turn-a", speaker_id: str = "speaker-a") -> dict:
    speaker = anonymous_speaker_annotation(SCOPE, speaker_id, UVC_SESSION)
    return {
        "type": "qqcall_group_speech_result",
        "scope": dict(SCOPE),
        "session_ref": group_realtime_provider.group_audio_session_id(SCOPE),
        "session_identity": dict(UVC_SESSION),
        "source_id": "main",
        "speaker_annotation": speaker,
        "speech_result": {
            "turn_id": turn_id,
            "text": f"Core ASR receipt text {turn_id}",
            "voice_base64": VOICE_BASE64,
            "precomputed_asr_result_id": f"asr-result-{turn_id}",
            "asr_scope": f"uvc:receipt:{turn_id}",
        },
    }


def _message(*, turn_id: str = "turn-a", speaker_id: str = "speaker-a") -> MessageBase:
    return build_qqcall_group_voice_message(
        _envelope(turn_id=turn_id, speaker_id=speaker_id), REQUESTER, "Study group"
    )


def _annotation(message: MessageBase) -> RealtimeSession:
    config = message.message_info.additional_config
    return RealtimeSession(**config["realtime_session"])


def _chat_manager_patch():
    manager = SimpleNamespace(get_stream_id=Mock(return_value=CHAT_ID))
    return patch(
        "src.chat.message_receive.chat_stream.get_chat_manager",
        return_value=manager,
    ), manager


class QQCallGroupMessageBuilderTests(unittest.TestCase):
    def test_builds_two_anonymous_speakers_in_one_canonical_qq_group(self):
        first = _message(turn_id="turn-a", speaker_id="cluster-a")
        second = _message(turn_id="turn-b", speaker_id="cluster-b")
        first_config = first.message_info.additional_config
        second_config = second.message_info.additional_config

        self.assertEqual(first.message_info.platform, "qq")
        self.assertEqual(first.message_info.group_info, GroupInfo("qq", GROUP_ID, "Study group"))
        self.assertIsNone(first.message_info.user_info)
        self.assertIsNone(first.message_info.sender_info)
        self.assertIsNone(first.raw_message)
        self.assertEqual(first.message_segment, Seg("voice", VOICE_BASE64))
        self.assertEqual(first.message_info.message_id, f"qqcall:group-voice:{first_config['realtime_session']['session_id']}:turn-a")
        self.assertEqual(first.message_info.message_id.split(":")[2], second.message_info.message_id.split(":")[2])
        self.assertNotEqual(first_config["realtime_speaker"]["speaker_ref"], second_config["realtime_speaker"]["speaker_ref"])
        self.assertEqual(first_config["realtime_speaker"]["label"], f"语音用户 {first_config['realtime_speaker']['speaker_ref'][4:]}")
        self.assertEqual(first_config["precomputed_asr_result_id"], "asr-result-turn-a")
        self.assertEqual(first_config["precomputed_asr_scope"], "uvc:receipt:turn-a")
        self.assertNotIn("Core ASR receipt text turn-a", first_config.values())
        self.assertNotIn("processed_plain_text", first_config)
        self.assertEqual(first_config["delivery_target"]["kind"], "qqcall_group")
        self.assertEqual(first_config["delivery_target"]["requester_user_id"], REQUESTER)
        self.assertEqual(first_config["delivery_target"]["groupId"], GROUP_ID)
        self.assertEqual(first_config["runtime_capabilities"], {
            "mcp_enabled": False,
            "schema_version": 1,
            "planner_bypass": True,
            "reply_delivery": "tts_text",
            "voice_stream": True,
            "tts_language": "zh",
        })

    def test_rejects_bad_callback_scope_hash_claim_source_and_payload(self):
        invalidators = [
            lambda e: e.update(session_ref="0" * 64),
            lambda e: e.update(source_id="foreign"),
            lambda e: e["session_identity"].update(source_id="foreign"),
            lambda e: e["speaker_annotation"].update(label="管理员"),
            lambda e: e["speech_result"].update(turn_id="bad turn"),
            lambda e: e["speech_result"].update(voice_base64="%%%"),
            lambda e: e["speech_result"].update(precomputed_asr_result_id=""),
        ]
        for invalidate in invalidators:
            with self.subTest(invalidator=invalidators.index(invalidate)):
                envelope = copy.deepcopy(_envelope())
                invalidate(envelope)
                with self.assertRaises(ValueError):
                    build_qqcall_group_voice_message(envelope, REQUESTER, "Study group")

    def test_requester_is_acl_metadata_only_and_is_never_a_sender(self):
        message = _message()
        self.assertIsNone(message.message_info.user_info)
        self.assertIsNone(message.message_info.sender_info)
        self.assertEqual(
            message.message_info.additional_config["qqcall_group_requester_user_id"],
            REQUESTER,
        )


class QQCallGroupRealtimeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_wav_fallback_requires_native_proof_and_rejects_partial_receipt(self):
        envelope = _envelope()
        envelope["speech_result"].update(text=None, precomputed_asr_result_id=None)
        message = build_qqcall_group_voice_message(envelope, REQUESTER, "Study group")
        config = message.message_info.additional_config
        self.assertNotIn("precomputed_asr_result_id", config)
        self.assertEqual(config["precomputed_asr_scope"], envelope["speech_result"]["asr_scope"])
        self.assertEqual(message.message_segment, Seg("voice", VOICE_BASE64))
        for active in (True, False):
            request = AsyncMock(return_value={"active": active})
            with patch.object(group_realtime_provider, "request_qqcall", request):
                self.assertEqual(await group_realtime_provider.verify_qqcall_group_realtime_message(
                    message, CHAT_ID, _annotation(message)), active)
            self.assertEqual(request.await_count, 1)
        config.pop("precomputed_asr_scope")
        request = AsyncMock(return_value={"active": True})
        with patch.object(group_realtime_provider, "request_qqcall", request):
            self.assertFalse(await group_realtime_provider.verify_qqcall_group_realtime_message(
                message, CHAT_ID, _annotation(message)))
        request.assert_not_awaited()

    def setUp(self):
        self.manager_patch, self.chat_manager = _chat_manager_patch()
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)
        self.enabled_patch = patch.object(group_realtime_provider, "qqcall_enabled", return_value=True)
        self.enabled_patch.start()
        self.addCleanup(self.enabled_patch.stop)

    async def test_requires_exact_live_group_audio_proof_and_uses_requester_for_acl(self):
        message = _message()
        annotation = _annotation(message)
        status = AsyncMock(return_value={"active": True})
        with patch.object(group_realtime_provider, "request_qqcall", status):
            self.assertTrue(
                await group_realtime_provider.verify_qqcall_group_realtime_message(
                    message, CHAT_ID, annotation
                )
            )
        status.assert_awaited_once_with(
            "group_audio_status",
            platform="qq",
            peer_user_id=REQUESTER,
            group_id=GROUP_ID,
            scope_group=SCOPE,
            audio_claim={
                "turn_id": "turn-a",
                "uvc_session": UVC_SESSION,
                "speaker_ref": message.message_info.additional_config["realtime_speaker"]["speaker_ref"],
            },
            timeout=2.5,
        )
        self.assertNotEqual(REQUESTER, message.message_info.additional_config["realtime_speaker"]["speaker_ref"])

    async def test_false_status_errors_and_acl_revocation_never_authorize(self):
        for result in ({"active": False}, {"active": True, "extra": True}, {"active": 1}):
            with self.subTest(result=result):
                message = _message()
                with patch.object(group_realtime_provider, "request_qqcall", AsyncMock(return_value=result)):
                    self.assertFalse(
                        await group_realtime_provider.verify_qqcall_group_realtime_message(
                            message, CHAT_ID, _annotation(message)
                        )
                    )

        for error in (RuntimeError("transport error"), QQCallAPIError("group_not_admitted")):
            with self.subTest(error=type(error).__name__):
                message = _message()
                with patch.object(
                    group_realtime_provider,
                    "request_qqcall",
                    AsyncMock(side_effect=error),
                ):
                    self.assertFalse(
                        await group_realtime_provider.verify_qqcall_group_realtime_message(
                            message, CHAT_ID, _annotation(message)
                        )
                    )

        message = _message()
        request = AsyncMock(return_value={"active": True})
        with (
            patch.object(group_realtime_provider, "request_qqcall", request),
            patch.object(group_realtime_provider, "qqcall_enabled", return_value=False),
        ):
            self.assertFalse(
                await group_realtime_provider.verify_qqcall_group_realtime_message(
                    message, CHAT_ID, _annotation(message)
                )
            )
        request.assert_not_awaited()

    async def test_rejects_foreign_scope_hash_claim_uvi_source_context_and_speaker_label(self):
        invalidators = (
            lambda m: m.message_info.additional_config["runtime_capabilities"].update(mcp_enabled=True),
            lambda m: m.message_info.additional_config["runtime_capabilities"].pop("mcp_enabled"),
            lambda m: m.message_info.additional_config["runtime_capabilities"].update(tts_language="ja"),
            lambda m: m.message_info.additional_config["qqcall_group_scope"].update(roomId="foreign-room"),
            lambda m: m.message_info.additional_config["realtime_session"].update(session_id="0" * 64),
            lambda m: m.message_info.additional_config["qqcall_group_audio_claim"]["uvc_session"].update(source_id="foreign"),
            lambda m: m.message_info.additional_config["delivery_target"].update(requester_user_id="98765"),
            lambda m: m.message_info.additional_config["realtime_speaker"].update(label="语音用户 forged"),
            lambda m: setattr(m.message_info, "platform", "discord"),
            lambda m: setattr(m.message_info.group_info, "group_id", "987654321"),
            lambda m: setattr(m, "message_segment", Seg("text", "plain text")),
            lambda m: setattr(m.message_info, "user_info", SimpleNamespace(user_id=REQUESTER)),
            lambda m: m.message_info.additional_config.update(system_event={"malformed": True}),
        )
        for invalidate in invalidators:
            with self.subTest(invalidator=invalidators.index(invalidate)):
                message = _message()
                invalidate(message)
                request = AsyncMock(return_value={"active": True})
                with patch.object(group_realtime_provider, "request_qqcall", request):
                    self.assertFalse(
                        await group_realtime_provider.verify_qqcall_group_realtime_message(
                            message, CHAT_ID, _annotation(message)
                        )
                    )
                request.assert_not_awaited()

    async def test_requester_or_scope_mutation_during_native_proof_rejects(self):
        for key, replacement in (
            ("qqcall_group_requester_user_id", "98765"),
            ("qqcall_group_scope", dict(SCOPE, roomId="changed-room")),
        ):
            with self.subTest(key=key):
                message = _message()
                entered = asyncio.Event()
                release = asyncio.Event()

                async def slow_status(*_args, **_kwargs):
                    entered.set()
                    await release.wait()
                    return {"active": True}

                with patch.object(group_realtime_provider, "request_qqcall", slow_status):
                    proof = asyncio.create_task(
                        group_realtime_provider.verify_qqcall_group_realtime_message(
                            message, CHAT_ID, _annotation(message)
                        )
                    )
                    await entered.wait()
                    message.message_info.additional_config[key] = replacement
                    release.set()
                    self.assertFalse(await proof)

        enabled_values = iter((True, False))
        message = _message()
        with (
            patch.object(group_realtime_provider, "request_qqcall", AsyncMock(return_value={"active": True})),
            patch.object(group_realtime_provider, "qqcall_enabled", side_effect=lambda: next(enabled_values)),
        ):
            self.assertFalse(
                await group_realtime_provider.verify_qqcall_group_realtime_message(
                    message, CHAT_ID, _annotation(message)
                )
            )

    async def test_generic_ingress_records_and_revalidates_actorless_tts_reply_without_planner(self):
        import asyncio

        message = _message()
        message_info = message.message_info
        manager = RealtimeIngressManager()
        manager.register_provider(
            RealtimeProvider(
                "qqcall_group",
                group_realtime_provider.verify_qqcall_group_realtime_message,
                enabled=lambda: True,
            )
        )
        with patch.object(group_realtime_provider, "request_qqcall", AsyncMock(return_value={"active": True})):
            decision = await manager.inspect_message(message, CHAT_ID)
            self.assertTrue(decision.declared)
            self.assertTrue(decision.authorized)
            self.assertIsNone(message_info.user_info)
            self.assertTrue(
                manager.record_message(
                    decision,
                    StoredMessageRef(501, CHAT_ID, message_info.message_id, message_info.time),
                )
            )

            stored_config = json.dumps(message_info.additional_config, ensure_ascii=False)
            row = DatabaseMessages(
                message_id=message_info.message_id,
                time=message_info.time,
                chat_id=CHAT_ID,
                processed_plain_text="Core ASR receipt result",
                display_message="Core ASR receipt result",
                additional_config=stored_config,
                chat_info_stream_id=CHAT_ID,
                chat_info_platform="qq",
                chat_info_group_id=GROUP_ID,
                chat_info_group_name="Study group",
                chat_info_group_platform="qq",
            )
            row.id = 501
            self.assertTrue(await manager.revalidate_message(row, CHAT_ID))
            self.assertTrue(manager.is_authorized_anonymous_speaker_message(row))

            with patch("src.chat.realtime_ingress.realtime_ingress_manager", manager):
                batch = classify_runtime_message_batch([row])
                self.assertEqual(batch.direct_messages, (row,))
                self.assertEqual(batch.planner_messages, ())
                self.assertEqual(runtime_capabilities_from_message(row).reply_delivery, "tts_text")

                from src.chat.replyer.group_generator import DefaultReplyer
                import src.chat.replyer.group_generator as group_generator_module

                replyer = object.__new__(DefaultReplyer)
                admin = SimpleNamespace(user_id="cached-admin", user_nickname="Cached Admin", platform="qq")
                replyer.chat_stream = SimpleNamespace(
                    stream_id=CHAT_ID,
                    platform="qq",
                    user_info=admin,
                    group_info=SimpleNamespace(group_id=GROUP_ID, group_name="Study group"),
                    context=SimpleNamespace(message=SimpleNamespace(message_info=SimpleNamespace(
                        message_id="prior-admin-message",
                        sender_info=admin,
                        user_info=admin,
                        additional_config={"disable_tools": True},
                    ))),
                )
                replyer.request_type = "replyer"
                replyer.build_expression_habits = AsyncMock(return_value=("", []))
                replyer.build_relation_info = AsyncMock(return_value="")
                replyer.build_tool_info = AsyncMock(return_value="")
                replyer.get_prompt_info = AsyncMock(return_value="")
                replyer.build_actions_prompt = AsyncMock(return_value="")
                replyer.build_personality_prompt = AsyncMock(return_value="")
                replyer._build_mid_term_memory_block = AsyncMock(return_value="")
                replyer.build_keywords_reaction_prompt = AsyncMock(return_value="")
                replyer.build_split_chat_history_prompts = Mock(return_value=("", ""))
                person_probe = Mock()
                with (
                    patch.object(group_generator_module.global_config.chat, "get_max_context_size", return_value=30),
                    patch.object(group_generator_module.global_config.mood, "enable_mood", False),
                    patch.object(group_generator_module.global_config.bot, "qq_account", "bot-account"),
                    patch.object(group_generator_module.global_config.bot, "platform", "qq"),
                    patch.object(group_generator_module.global_config.bot, "nickname", "NachoBot"),
                    patch.object(group_generator_module.global_config.personality, "reply_style", "自然"),
                    patch.object(group_generator_module.global_config.personality, "gift_reaction_prompt", ""),
                    patch.object(group_generator_module, "get_latest_session_name", return_value="session"),
                    patch.object(group_generator_module, "get_stepped_limit", return_value=10),
                    patch.object(group_generator_module, "get_raw_msg_before_timestamp_with_chat", return_value=[]),
                    patch.object(group_generator_module, "build_readable_messages", return_value=""),
                    patch.object(group_generator_module, "build_memory_retrieval_prompt", new=AsyncMock(return_value="")),
                    patch.object(group_generator_module, "replace_user_references", side_effect=lambda text, *_a, **_k: text),
                    patch.object(group_generator_module, "guard_user_content", side_effect=lambda text, *_a: (text, False, [])),
                    patch.object(group_generator_module, "Person", new=person_probe),
                    patch.object(group_generator_module.global_prompt_manager, "format_prompt", new=AsyncMock(return_value="prompt")),
                    patch("src.chat.offline_recovery.offline_recovery_manager") as recovery_manager,
                ):
                    recovery_manager.filter_pending_recovery_messages.side_effect = lambda _chat_id, messages: list(messages)
                    await replyer.build_prompt_reply_context(reply_message=row)
                self.assertEqual(replyer.build_tool_info.await_args.args[1], "")
                self.assertEqual(replyer.build_tool_info.await_args.kwargs["user_id"], "")
                self.assertTrue(replyer.build_tool_info.await_args.kwargs["actorless_anonymous"])
                person_probe.assert_not_called()

    async def test_plugin_registers_group_provider_independently_and_unloads_both(self):
        registrations = []
        removals = []

        def register(name, verifier, **kwargs):
            registrations.append((name, verifier, kwargs))

        with (
            patch("src.plugin_system.apis.realtime_api.register_realtime_provider", side_effect=register),
            patch("src.plugin_system.apis.realtime_api.unregister_realtime_provider", side_effect=removals.append),
            patch("plugins.qqcall_plugin.realtime_provider.is_qqcall_enabled", return_value=True),
        ):
            qqcall_plugin.QQCallPlugin._register_realtime_provider()

        self.assertEqual({name for name, _verifier, _kwargs in registrations}, {"qqcall", "qqcall_group"})
        private = next(item for item in registrations if item[0] == "qqcall")
        group = next(item for item in registrations if item[0] == "qqcall_group")
        self.assertIsNotNone(private[2].get("lifecycle_verifier"))
        self.assertNotIn("lifecycle_verifier", group[2])

        original_import = __import__

        def fail_group_import(name, *args, **kwargs):
            if name == "plugins.qqcall_plugin.group_realtime_provider":
                raise ImportError("simulated partial group module install")
            return original_import(name, *args, **kwargs)

        registrations.clear()
        removals.clear()
        with (
            patch("src.plugin_system.apis.realtime_api.register_realtime_provider", side_effect=register),
            patch("src.plugin_system.apis.realtime_api.unregister_realtime_provider", side_effect=removals.append),
            patch("plugins.qqcall_plugin.realtime_provider.is_qqcall_enabled", return_value=True),
            patch("builtins.__import__", side_effect=fail_group_import),
        ):
            qqcall_plugin.QQCallPlugin._register_realtime_provider()
        self.assertEqual([name for name, _verifier, _kwargs in registrations], ["qqcall"])
        self.assertIn("qqcall_group", removals)

        registrations.clear()
        removals.clear()
        with (
            patch("src.plugin_system.apis.realtime_api.register_realtime_provider", side_effect=register),
            patch("src.plugin_system.apis.realtime_api.unregister_realtime_provider", side_effect=removals.append),
            patch("plugins.qqcall_plugin.realtime_provider.is_qqcall_enabled", return_value=False),
        ):
            qqcall_plugin.QQCallPlugin._register_realtime_provider()
        self.assertEqual(registrations, [])
        self.assertCountEqual(removals, ["qqcall", "qqcall_group"])

        removals.clear()
        plugin = object.__new__(qqcall_plugin.QQCallPlugin)
        with patch(
            "src.plugin_system.apis.realtime_api.unregister_realtime_provider",
            side_effect=removals.append,
        ):
            plugin.on_plugin_unload()
        self.assertEqual(removals, ["qqcall", "qqcall_group"])

    async def test_private_provider_still_rejects_a_group_voice_message(self):
        message = _message()
        with patch.object(
            group_realtime_provider,
            "request_qqcall",
            AsyncMock(side_effect=AssertionError("group voice must not use private-call proof")),
        ):
            self.assertFalse(
                await verify_qqcall_realtime_message(message, CHAT_ID, _annotation(message))
            )


if __name__ == "__main__":
    unittest.main()
