import itertools
import unittest
from types import SimpleNamespace

from discord_policy import accept_formats


class DiscordCapabilityTests(unittest.TestCase):
    def test_tts_text_tracks_materialized_speech_paths(self):
        for voice_enabled, text_tts_enabled in itertools.product((False, True), repeat=2):
            config = SimpleNamespace(
                voice=SimpleNamespace(
                    enabled=voice_enabled,
                    use_tts=text_tts_enabled,
                )
            )
            for voice_context in (False, True):
                with self.subTest(
                    voice_enabled=voice_enabled,
                    text_tts_enabled=text_tts_enabled,
                    voice_context=voice_context,
                ):
                    formats = accept_formats(config, voice_context=voice_context)
                    speech_output_enabled = voice_enabled if voice_context else text_tts_enabled
                    self.assertEqual("tts_text" in formats, speech_output_enabled)
                    self.assertEqual("voice" in formats, speech_output_enabled)
                    self.assertEqual("voice_stream" in formats, voice_context and voice_enabled)
                    self.assertIn("voicefile", formats)


if __name__ == "__main__":
    unittest.main()
