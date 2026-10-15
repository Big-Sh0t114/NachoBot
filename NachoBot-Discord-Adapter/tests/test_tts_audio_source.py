from __future__ import annotations

import unittest
from types import SimpleNamespace

from discord.utils import MISSING
from discord.voice.client import VoiceClient as PinnedVoiceClient

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
        channel_id = 7
        generation = "voice-generation-1"

        class Player:
            def __init__(self, source):
                self.source = source
                self.stopped = 0

            def is_playing(self):
                return True

            def is_paused(self):
                return False

            def stop(self):
                self.stopped += 1

        class Reader:
            def __init__(self):
                self.listening = True
                self.stopped = 0

            def is_listening(self):
                return self.listening

            def stop(self):
                self.stopped += 1
                self.listening = False

        class Future:
            def __init__(self):
                self.value = MISSING

            def done(self):
                return self.value is not MISSING

            def set_result(self, value):
                self.value = value

        class VoiceClient:
            def __init__(self):
                self.source = None
                self._player = None
                self._player_future = Future()
                self._reader = Reader()
                self._set_future_result_if_pending = (
                    PinnedVoiceClient._set_future_result_if_pending
                )
                self.loop = SimpleNamespace(
                    call_soon_threadsafe=lambda callback, *args: callback(*args)
                )
                self.stop_calls = 0

            def is_connected(self):
                return True

            def is_playing(self):
                return PinnedVoiceClient.is_playing(self)

            def is_paused(self):
                return PinnedVoiceClient.is_paused(self)

            def is_recording(self):
                return PinnedVoiceClient.is_recording(self)

            def play(self, source, *, after):
                self.source = source
                self._player = Player(source)
                self.after = after

            def stop(self):
                self.stop_calls += 1
                PinnedVoiceClient.stop(self)

        class Harness:
            start_tts_stream = NachoDiscordBot.start_tts_stream
            feed_tts_stream = NachoDiscordBot.feed_tts_stream
            end_tts_stream = NachoDiscordBot.end_tts_stream
            abort_tts_stream = NachoDiscordBot.abort_tts_stream

            def __init__(self, vc):
                self.vc = vc
                self.tts_streams = {}
                self.session = SimpleNamespace(
                    channel_id=channel_id,
                    generation=generation,
                    voice_client=vc,
                    current_audio=None,
                    interrupted_audio=None,
                    current_playback_attempt=None,
                )
                self.loop = SimpleNamespace(call_soon_threadsafe=lambda callback: callback())

            def get_voice_session(self, requested_channel_id, requested_generation=None):
                if requested_channel_id != channel_id:
                    return None
                if requested_generation not in (None, generation):
                    return None
                return self.session

            def _play_next(self, session, error=None):
                pass

        vc = VoiceClient()
        bot = Harness(vc)
        reader = vc._reader
        player_future = vc._player_future
        self.assertTrue(
            bot.start_tts_stream(channel_id, generation, "stream", 24_000, 1, 2)
        )
        self.assertTrue(
            bot.feed_tts_stream(
                channel_id, generation, "stream", 0, b"\x01\x00" * 2_400
            )
        )
        self.assertTrue(bot.end_tts_stream(channel_id, generation, "stream"))
        self.assertEqual(len(vc.source.read()), DISCORD_FRAME_BYTES)
        player = vc._player
        self.assertTrue(bot.abort_tts_stream(channel_id, "stream"))
        self.assertEqual(vc.stop_calls, 0)
        self.assertEqual(player.stopped, 1)
        self.assertEqual(vc._reader.stopped, 0)
        self.assertIs(vc._reader, reader)
        self.assertTrue(vc.is_recording())
        self.assertIsNone(vc._player)
        self.assertIsNone(vc._player_future)
        self.assertTrue(player_future.done())
        self.assertIsNone(player_future.value)
        self.assertEqual(vc.source.read(), b"")


if __name__ == "__main__":
    unittest.main()
