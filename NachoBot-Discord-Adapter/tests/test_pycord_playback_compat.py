import asyncio
import unittest
from types import SimpleNamespace

from discord.utils import MISSING
from discord.voice.client import VoiceClient

from discord_voice_compat import stop_playback_only


class PlayerSpy:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class ReaderSpy:
    def __init__(self):
        self.listening = True
        self.stop_count = 0

    def is_listening(self):
        return self.listening

    def stop(self):
        self.stop_count += 1
        self.listening = False


def _native_shaped_voice_client(loop):
    return SimpleNamespace(
        _player=PlayerSpy(),
        _player_future=loop.create_future(),
        _reader=ReaderSpy(),
        _set_future_result_if_pending=VoiceClient._set_future_result_if_pending,
        loop=loop,
    )


class PycordPlaybackCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_native_stop_really_stops_the_receive_reader(self):
        """Keep the regression grounded in the exact installed Pycord API."""
        voice_client = _native_shaped_voice_client(asyncio.get_running_loop())
        reader = voice_client._reader
        player_future = voice_client._player_future

        VoiceClient.stop(voice_client)

        self.assertTrue(voice_client._player is None)
        self.assertEqual(reader.stop_count, 1)
        self.assertFalse(VoiceClient.is_recording(voice_client))
        self.assertIs(voice_client._reader, MISSING)
        await asyncio.sleep(0)
        self.assertTrue(player_future.done())
        self.assertIsNone(player_future.result())
        self.assertIsNone(voice_client._player_future)

    async def test_output_only_stop_matches_playback_cleanup_and_preserves_reader(self):
        voice_client = _native_shaped_voice_client(asyncio.get_running_loop())
        player = voice_client._player
        player_future = voice_client._player_future
        reader = voice_client._reader

        stop_playback_only(voice_client)

        self.assertTrue(player.stopped)
        self.assertIsNone(voice_client._player)
        self.assertIsNone(voice_client._player_future)
        self.assertIs(voice_client._reader, reader)
        self.assertEqual(reader.stop_count, 0)
        self.assertTrue(VoiceClient.is_recording(voice_client))
        await asyncio.sleep(0)
        self.assertTrue(player_future.done())
        self.assertIsNone(player_future.result())


if __name__ == "__main__":
    unittest.main()
