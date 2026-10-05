import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, Mock

from adapter import DiscordAdapter
from identity_map import IdentityMap


USER_ID = 123456789012345678
CHANNEL_ID = 423456789012345678
GUILD_ID = 623456789012345678


def _runtime_capabilities_module():
    module_name = "discord_reply_contract_runtime_capabilities"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = (
        Path(__file__).resolve().parents[2]
        / "NachoBot"
        / "src"
        / "chat"
        / "runtime_capabilities.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the pure runtime capability contract")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class DiscordReplyContractTests(TestCase):
    def test_voice_input_requests_core_plain_tts_reply(self):
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        adapter.config = SimpleNamespace(
            voice=SimpleNamespace(enabled=True, sample_rate=48_000),
            prompts=SimpleNamespace(
                planner_prompt='Plan with {planner_var}: {"action":"reply"}',
                replyer_prompt="Speak naturally with {identity}.",
                variables={"planner_var": "fixture"},
            ),
        )
        adapter.identity_map = IdentityMap.empty()
        adapter.bot = SimpleNamespace(_session_is_current=Mock(return_value=True))
        adapter.logger = Mock()
        adapter.router = SimpleNamespace(send_message=AsyncMock())
        session = SimpleNamespace(
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
            generation="voice-generation-1",
            voice_client=SimpleNamespace(
                guild=SimpleNamespace(get_member=lambda user_id: None),
                channel=SimpleNamespace(name="voice"),
            ),
        )

        asyncio.run(
            adapter.handle_speech_recognized(
                session=session,
                native_user_id=USER_ID,
                voice_data="input-audio",
                capture_id="capture-1",
                scope="dsc_reply_contract_scope",
            )
        )

        message = adapter.router.send_message.await_args.args[0]
        capabilities = message.message_info.additional_config["runtime_capabilities"]
        self.assertEqual(capabilities["identity_mode"], "external")
        self.assertIs(capabilities["planner_bypass"], True)
        self.assertEqual(capabilities["reply_delivery"], "tts_text")
        self.assertEqual(capabilities["tts_language"], "zh")
        self.assertTrue(capabilities["voice_stream"])
        self.assertNotIn("disable_tools", message.message_info.additional_config)
        self.assertNotIn("tool_mode", capabilities)
        self.assertNotIn("web_search_mode", capabilities)
        self.assertNotIn("memory_retrieval", capabilities)
        self.assertNotIn("knowledge_retrieval", capabilities)
        self.assertEqual(message.message_info.platform, "discord")
        self.assertEqual(message.message_info.user_info.user_id, str(USER_ID))
        self.assertEqual(
            message.message_info.additional_config["delivery_target"],
            {
                "schema_version": 1,
                "transport": "discord",
                "channel_id": str(CHANNEL_ID),
                "user_id": str(USER_ID),
                "guild_id": str(GUILD_ID),
                "mode": "voice",
                "voice_generation": session.generation,
            },
        )
        self.assertEqual(message.message_segment.type, "voice")
        self.assertEqual(message.message_segment.data, "input-audio")
        self.assertEqual(
            message.message_info.template_info.template_items,
            {
                "planner_prompt": 'Plan with fixture: {{"action":"reply"}}',
                "replyer_prompt": "Speak naturally with {identity}.",
            },
        )
        routed_message = {
            "additional_config": message.message_info.additional_config,
            "user_info": message.message_info.user_info,
        }
        batch = _runtime_capabilities_module().classify_runtime_message_batch(
            [routed_message]
        )
        self.assertEqual(batch.direct_messages, (routed_message,))
        self.assertEqual(batch.planner_messages, ())

    def test_text_and_slash_capabilities_keep_planner_routing(self):
        classify = _runtime_capabilities_module().classify_runtime_message_batch
        for route in ("text", "slash"):
            with self.subTest(route=route):
                message = {
                    "user_info": {"user_id": f"{route}-user"},
                    "additional_config": {
                        "runtime_capabilities": {
                            "schema_version": 1,
                            "reply_delivery": "chunked",
                            "voice_stream": False,
                        }
                    },
                }
                batch = classify([message])
                self.assertEqual(batch.direct_messages, ())
                self.assertEqual(batch.planner_messages, (message,))

    def test_voice_receipt_is_forwarded_only_when_nonempty(self):
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        adapter.config = SimpleNamespace(
            voice=SimpleNamespace(enabled=True, sample_rate=48_000),
            prompts=SimpleNamespace(
                planner_prompt="", replyer_prompt="", variables={}
            ),
        )
        adapter.identity_map = IdentityMap.empty()
        adapter.bot = SimpleNamespace(_session_is_current=Mock(return_value=True))
        adapter.logger = Mock()
        adapter.router = SimpleNamespace(send_message=AsyncMock())
        session = SimpleNamespace(
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
            generation="voice-generation-2",
            voice_client=SimpleNamespace(
                guild=SimpleNamespace(get_member=lambda user_id: None),
                channel=SimpleNamespace(name="voice"),
            ),
        )

        asyncio.run(
            adapter.handle_speech_recognized(
                session=session,
                native_user_id=USER_ID,
                voice_data="input-audio",
                precomputed_asr_result_id="receipt-1",
                capture_id="capture-2",
                scope="dsc_receipt_contract_scope",
            )
        )
        message = adapter.router.send_message.await_args.args[0]
        self.assertEqual(message.message_info.platform, "discord")
        self.assertEqual(
            message.message_info.additional_config["precomputed_asr_result_id"],
            "receipt-1",
        )
        self.assertEqual(message.message_segment.type, "voice")
        self.assertEqual(message.message_segment.data, "input-audio")

        for invalid_receipt in (None, "", "  ", "x" * 257):
            asyncio.run(
                adapter.handle_speech_recognized(
                    session=session,
                    native_user_id=USER_ID,
                    voice_data="input-audio",
                    precomputed_asr_result_id=invalid_receipt,
                    capture_id="capture-3",
                    scope="dsc_receipt_contract_scope",
                )
            )
            message = adapter.router.send_message.await_args.args[0]
            self.assertNotIn(
                "precomputed_asr_result_id",
                message.message_info.additional_config,
            )

    def test_empty_voice_prompt_uses_default_speech_prompt(self):
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        adapter.config = SimpleNamespace(
            voice=SimpleNamespace(enabled=True, sample_rate=48_000),
            prompts=SimpleNamespace(
                planner_prompt="", replyer_prompt="  ", variables={}
            ),
        )
        adapter.identity_map = IdentityMap.empty()
        adapter.bot = SimpleNamespace(_session_is_current=Mock(return_value=True))
        adapter.logger = Mock()
        adapter.router = SimpleNamespace(send_message=AsyncMock())
        session = SimpleNamespace(
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
            generation="voice-generation-default-prompt",
            voice_client=SimpleNamespace(
                guild=SimpleNamespace(get_member=lambda user_id: None),
                channel=SimpleNamespace(name="voice"),
            ),
        )

        asyncio.run(
            adapter.handle_speech_recognized(
                session=session,
                native_user_id=USER_ID,
                voice_data="input-audio",
                capture_id="capture-default-prompt",
                scope="dsc_default_prompt_scope",
            )
        )
        message = adapter.router.send_message.await_args.args[0]
        prompt = message.message_info.template_info.template_items["replyer_prompt"]
        self.assertIn("只输出一段自然、口语化的中文回复", prompt)
        self.assertIn("{identity}", prompt)
        self.assertIn("{background_dialogue_prompt}", prompt)
        self.assertIn("{tool_info_block}", prompt)
        self.assertNotIn('{"reply"', prompt)

    def test_live_prompt_requests_one_plain_natural_voice_utterance(self):
        prompt = Path(__file__).parents[1].joinpath("config.toml.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("只输出一段自然、口语化的中文回复", prompt)
        self.assertIn("控制在50字以内", prompt)
        self.assertIn("不要输出JSON、Markdown、额外分析、颜文字或表情符号", prompt)
        self.assertNotIn('{"reply"', prompt)
        self.assertNotIn('"tts_text"', prompt)


if __name__ == "__main__":
    import unittest

    unittest.main()
