from __future__ import annotations

import unittest
from types import SimpleNamespace

from discord_client import NachoDiscordBot
from tts_audio_source import DISCORD_FRAME_BYTES, PCMStreamSource


class DiscordTTSStreamSourceTests(unittest.TestCase):
    def test_24k_mono_chunk_becomes_48k_stereo_frames(self):
        source = PCMStreamSource("stream-1", 24_000, 1)
        self.assertEqual(source.read(), b"\x00" * DISCORD_FRAME_BYTES)
        source.feed(0, b"\x01\x00" * 2_400)
        source.finish()

        frames = []
        while frame := source.read():
            frames.append(frame)

        self.assertEqual(len(frames), 5)
        self.assertTrue(all(len(frame) == DISCORD_FRAME_BYTES for frame in frames))
        self.assertEqual(frames[0][:4], b"\x01\x00\x01\x00")

    def test_sequence_and_abort_discard_buffer(self):
        source = PCMStreamSource("stream-2", 48_000, 2)
        with self.assertRaises(ValueError):
            source.feed(1, b"\x01\x00" * 4)
        source.feed(0, b"\x01\x00" * 1_920)
        with self.assertRaises(ValueError):
            source.feed(0, b"\x01\x00" * 4)
        source.abort()
        self.assertEqual(source.read(), b"")
        with self.assertRaises(ValueError):
            source.feed(1, b"\x01\x00" * 4)

    def test_bot_plays_stream_source_and_interrupts_it(self):
        class VoiceClient:
            def __init__(self):
                self.source = None
                self.playing = False
                self.stops = 0

            def is_connected(self):
                return True

            def is_playing(self):
                return self.playing

            def play(self, source, *, after):
                self.source = source
                self.playing = True
                self.after = after

            def stop(self):
                self.playing = False
                self.stops += 1

        class Harness:
            start_tts_stream = NachoDiscordBot.start_tts_stream
            feed_tts_stream = NachoDiscordBot.feed_tts_stream
            end_tts_stream = NachoDiscordBot.end_tts_stream
            abort_tts_stream = NachoDiscordBot.abort_tts_stream

            def __init__(self, vc):
                self.vc = vc
                self.tts_streams = {}
                self.loop = SimpleNamespace(call_soon_threadsafe=lambda callback: callback())

            def get_guild(self, guild_id):
                return SimpleNamespace(voice_client=self.vc)

            def _play_next(self, guild_id, error=None):
                pass

        vc = VoiceClient()
        bot = Harness(vc)
        self.assertTrue(bot.start_tts_stream(7, "stream", 24_000, 1, 2))
        self.assertTrue(bot.feed_tts_stream(7, "stream", 0, b"\x01\x00" * 2_400))
        self.assertTrue(bot.end_tts_stream(7, "stream"))
        self.assertEqual(len(vc.source.read()), DISCORD_FRAME_BYTES)
        self.assertTrue(bot.abort_tts_stream(7, "stream"))
        self.assertEqual(vc.stops, 1)
        self.assertEqual(vc.source.read(), b"")


if __name__ == "__main__":
    unittest.main()
