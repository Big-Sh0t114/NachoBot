import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, Mock

from adapter import DiscordAdapter


class DiscordReplyContractTests(TestCase):
    def test_voice_input_requests_core_json_tts_reply(self):
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        adapter.config = SimpleNamespace(
            voice=SimpleNamespace(sample_rate=48_000),
            prompts=SimpleNamespace(
                planner_prompt="", replyer_prompt="", variables={}
            ),
        )
        adapter.logger = Mock()
        adapter.router = SimpleNamespace(send_message=AsyncMock())

        asyncio.run(
            adapter.handle_speech_recognized(
                guild_id=7,
                user_id=11,
                voice_data="input-audio",
                user_name="听众",
            )
        )

        message = adapter.router.send_message.await_args.args[0]
        capabilities = message.message_info.additional_config["runtime_capabilities"]
        self.assertEqual(capabilities["reply_delivery"], "json_envelope")
        self.assertEqual(capabilities["tts_language"], "zh")
        self.assertEqual(message.message_segment.type, "voice")
        self.assertEqual(message.message_segment.data, "input-audio")

    def test_live_prompt_requires_reply_and_tts_text_fields(self):
        prompt = Path(__file__).parents[1].joinpath("config.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('"reply"', prompt)
        self.assertIn('"tts_text"', prompt)
        self.assertIn('{"reply"', prompt)
        self.assertNotIn('{{"reply"', prompt)
        self.assertIn("只输出一个 JSON 对象", prompt)


if __name__ == "__main__":
    import unittest

    unittest.main()
