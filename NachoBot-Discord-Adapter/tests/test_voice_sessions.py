import asyncio
import logging
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from discord.utils import MISSING
from discord.voice.client import VoiceClient

from discord_client import AudioItem, NachoDiscordBot, VoiceSession
from identity_map import IdentityMap


GUILD_ID = 623456789012345678
CHANNEL_ID = 423456789012345678


class FakeNativePlayer:
    def __init__(self, source=None):
        self.source = source
        self.playing = True
        self.paused = False
        self.stop_count = 0

    def is_playing(self):
        return self.playing and not self.paused

    def is_paused(self):
        return self.playing and self.paused

    def stop(self):
        self.stop_count += 1
        self.playing = False
        self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


class FakeNativeReader:
    def __init__(self):
        self.listening = True
        self.stop_count = 0

    def is_listening(self):
        return self.listening

    def stop(self):
        self.stop_count += 1
        self.listening = False


class FakeVoiceClient:
    def __init__(self):
        self.channel = SimpleNamespace(id=CHANNEL_ID, name="voice")
        self.guild = SimpleNamespace(id=GUILD_ID, get_member=lambda user_id: None)
        self.ws = object()
        self.session_id = "gateway-session-1"
        self.connected = True
        self.fail_play = False
        self.recordings = []
        self.played = []
        self.stop_count = 0
        self.loop = asyncio.get_running_loop()
        self._player = None
        self._player_future = None
        self._reader = MISSING
        self.after = None
        self.source = None

    def is_connected(self):
        return self.connected

    def is_recording(self):
        return bool(VoiceClient.is_recording(self))

    def is_playing(self):
        return VoiceClient.is_playing(self)

    def is_paused(self):
        return VoiceClient.is_paused(self)

    @property
    def recording(self):
        return self.is_recording()

    @property
    def playing(self):
        return self.is_playing()

    @playing.setter
    def playing(self, value):
        if value:
            if self._player is None:
                self._player = FakeNativePlayer(self.source)
        else:
            self._player = None

    @property
    def paused(self):
        return self.is_paused()

    @paused.setter
    def paused(self, value):
        if self._player is None:
            if value:
                self._player = FakeNativePlayer(self.source)
                self._player.paused = True
            return
        self._player.paused = bool(value)

    def start_recording(self, sink, callback):
        self._reader = FakeNativeReader()
        self.recordings.append((sink, callback))

    def stop_recording(self):
        if self._reader is not MISSING:
            self._reader.stop()
            self._reader = MISSING

    def play(self, source, *, after):
        if self.fail_play:
            raise RuntimeError("decoder launch failed")
        self.source = source
        self._player = FakeNativePlayer(source)
        self.played.append(source)
        self.after = after

    def stop(self):
        self.stop_count += 1
        # Match pinned Pycord: stop() halts output and the receive reader.
        VoiceClient.stop(self)

    def finish_playback(self, error=None):
        callback = self.after
        self._player = None
        self.source = None
        self.after = None
        if callback:
            callback(error)


class FakeSink:
    def __init__(self, **callbacks):
        self.callbacks = callbacks
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeCoreAudioStreams:
    def __init__(self):
        self.aborted = []

    async def abort(self, stream_id):
        self.aborted.append(stream_id)


def _audio_file(prefix):
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".wav")
    os.close(fd)
    result = Path(path)
    result.write_bytes(b"owned audio")
    return result


def _bare_bot():
    bot = NachoDiscordBot.__new__(NachoDiscordBot)
    bot.adapter_config = SimpleNamespace(
        voice=SimpleNamespace(enabled=True, sample_rate=48_000)
    )
    bot.voice_handler = SimpleNamespace()
    bot.logger = Mock(spec=logging.Logger)
    bot.transport_adapter = None
    bot.voice_sessions = {}
    bot.tts_streams = {}
    bot._active_sinks = set()
    bot._owned_temp_audio = set()
    bot.core_audio_streams = FakeCoreAudioStreams()
    bot.loop = asyncio.get_running_loop()
    bot.speech_callback = None
    bot.voice_handler.start_stream = Mock(return_value=True)
    bot.voice_handler.finish_stream = Mock(return_value=None)
    bot.voice_handler.abort_stream = Mock()
    bot.voice_handler.accept_pcm = Mock()
    return bot


def _session(bot, voice_client, *, generation="generation-1"):
    session = VoiceSession(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        generation=generation,
        voice_client=voice_client,
        voice_ws=voice_client.ws,
        voice_session_id=voice_client.session_id,
        queue=deque(),
    )
    bot.voice_sessions[GUILD_ID] = session
    return session


class DiscordVoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_channel_gateway_reconnect_replaces_stale_generation(self):
        for changed_field in ("ws", "session_id"):
            with self.subTest(changed_field=changed_field):
                bot = _bare_bot()
                voice_client = FakeVoiceClient()
                with patch(
                    "discord_client.SilenceDetectingSink", side_effect=FakeSink
                ):
                    old = await bot.start_listening(voice_client, GUILD_ID)
                    self.assertIsNotNone(old)
                    old_generation = old.generation
                    if changed_field == "ws":
                        voice_client.ws = object()
                    else:
                        voice_client.session_id = "gateway-session-2"

                    self.assertIsNone(
                        bot.get_voice_session(CHANNEL_ID, old_generation)
                    )
                    fresh = await bot.start_listening(voice_client, GUILD_ID)

                    self.assertIsNotNone(fresh)
                    self.assertIsNot(fresh, old)
                    self.assertNotEqual(fresh.generation, old_generation)
                    self.assertEqual(fresh.channel_id, CHANNEL_ID)
                    self.assertEqual(fresh.voice_session_id, voice_client.session_id)
                    self.assertTrue(old.closed)
                    await bot.invalidate_voice_session(GUILD_ID)

    async def test_disallowed_voice_user_is_rejected_before_capture_starts(self):
        bot = _bare_bot()
        bot.voice_handler.start_stream = Mock(return_value=True)
        voice_policy = SimpleNamespace(
            identity_map=IdentityMap.empty(),
            is_voice_user_allowed=Mock(return_value=False),
        )
        bot.transport_adapter = voice_policy
        voice_client = FakeVoiceClient()

        with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
            session = await bot.start_listening(voice_client, GUILD_ID)
            sink = voice_client.recordings[-1][0]

            accepted = sink.callbacks["on_capture_start_callback"](
                "capture-from-banned-user", 123456789012345679
            )

            self.assertFalse(accepted)
            voice_policy.is_voice_user_allowed.assert_called_once_with(
                123456789012345679, CHANNEL_ID
            )
            bot.voice_handler.start_stream.assert_not_called()
            await bot.invalidate_voice_session(GUILD_ID)

    async def test_waiting_playback_receipt_resolves_only_after_voice_client_starts(self):
        bot = _bare_bot()
        voice_client = FakeVoiceClient()
        session = _session(bot, voice_client)
        voice_client.playing = True  # An existing item owns the playback device.
        audio_file = _audio_file("discord-queued-")
        bot.own_temp_audio(str(audio_file))

        with patch("discord_client.discord.FFmpegPCMAudio", side_effect=lambda path, **_: path):
            waiting = asyncio.create_task(
                bot.speak(
                    CHANNEL_ID,
                    str(audio_file),
                    session.generation,
                    wait_until_started=True,
                )
            )
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())
            self.assertEqual([item.path for item in session.queue], [str(audio_file)])

            voice_client.playing = False
            bot._play_next(session)
            self.assertTrue(await asyncio.wait_for(waiting, timeout=1))

        self.assertEqual(len(voice_client.played), 1)
        self.assertEqual(voice_client.played[0], str(audio_file))
        self.assertTrue(voice_client.playing)
        self.assertTrue(audio_file.exists())
        await bot.invalidate_voice_session(GUILD_ID)
        self.assertFalse(audio_file.exists())

    async def test_enqueue_does_not_release_active_owned_audio_while_playing_or_paused(self):
        for state in ("playing", "paused"):
            with self.subTest(state=state):
                bot = _bare_bot()
                voice_client = FakeVoiceClient()
                session = _session(bot, voice_client)
                first_file = _audio_file("discord-active-owned-")
                bot.own_temp_audio(str(first_file))
                first = AudioItem(str(first_file), cleanup_owned=True)
                session.current_audio = first
                if state == "playing":
                    voice_client.playing = True
                else:
                    voice_client.paused = True
                session.queue.append(AudioItem("next.wav", cleanup_owned=False))

                bot._play_next(session)

                self.assertIs(session.current_audio, first)
                self.assertTrue(first_file.exists())
                self.assertEqual(voice_client.played, [])
                await bot.invalidate_voice_session(GUILD_ID)
                self.assertFalse(first_file.exists())

    async def test_bounded_queue_drops_oldest_and_receipt_means_play_started(self):
        bot = _bare_bot()
        voice_client = FakeVoiceClient()
        first_file = _audio_file("discord-current-owned-")
        bot.own_temp_audio(str(first_file))

        with patch("discord_client.discord.FFmpegPCMAudio", side_effect=lambda path, **_: path):
            session = _session(bot, voice_client)
            self.assertTrue(
                await bot.speak(
                    CHANNEL_ID,
                    str(first_file),
                    session.generation,
                    wait_until_started=True,
                )
            )
            first = session.current_audio
            waiters = [
                asyncio.create_task(
                    bot.speak(
                        CHANNEL_ID,
                        f"queued-{index}.wav",
                        session.generation,
                        cleanup=False,
                        wait_until_started=True,
                    )
                )
                for index in range(6)
            ]
            await asyncio.sleep(0)

            self.assertIs(session.current_audio, first)
            self.assertTrue(first_file.exists())
            self.assertEqual(
                [item.path for item in session.queue],
                [f"queued-{index}.wav" for index in range(1, 6)],
            )
            self.assertFalse(await waiters[0])
            self.assertFalse(waiters[1].done())

            voice_client.finish_playback()
            await asyncio.sleep(0)
            self.assertEqual(voice_client.played[-1], "queued-1.wav")
            self.assertEqual(len(voice_client.played), 2)
            self.assertTrue(await waiters[1])
            self.assertIsNotNone(session.current_audio)
            self.assertEqual(session.current_audio.path, "queued-1.wav")
            self.assertFalse(first_file.exists())
            self.assertTrue(all(not waiter.done() for waiter in waiters[2:]))

        for waiter in waiters[2:]:
            waiter.cancel()
        results = await asyncio.gather(*waiters[2:], return_exceptions=True)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        await bot.invalidate_voice_session(GUILD_ID)

    async def test_speech_interruption_preserves_reader_and_resumes_same_owned_audio(self):
        bot = _bare_bot()
        voice_client = FakeVoiceClient()
        audio_file = _audio_file("discord-interrupted-owned-")
        bot.own_temp_audio(str(audio_file))

        with patch("discord_client.discord.FFmpegPCMAudio", side_effect=lambda path, **_: path):
            with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
                session = await bot.start_listening(voice_client, GUILD_ID)
            self.assertTrue(
                await bot.speak(
                    CHANNEL_ID,
                    str(audio_file),
                    session.generation,
                    wait_until_started=True,
                )
            )
            first = session.current_audio
            first_after = voice_client.after
            reader = voice_client._reader
            sink = session.sink

            await sink.callbacks["on_speech_start_callback"](123456789012345679)

            self.assertIs(session.interrupted_audio, first)
            self.assertIs(session.current_audio, first)
            self.assertTrue(audio_file.exists())
            self.assertIs(voice_client._reader, reader)
            self.assertTrue(voice_client.is_recording())
            self.assertEqual(reader.stop_count, 0)

            self.assertTrue(
                sink.callbacks["on_capture_start_callback"](
                    "capture-one", 123456789012345679
                )
            )
            await sink.callbacks["callback"](
                123456789012345679,
                "captured-voice",
                None,
                "capture-one",
            )
            resumed_after = voice_client.after

            self.assertIs(session.current_audio, first)
            self.assertIsNone(session.interrupted_audio)
            self.assertEqual(voice_client.played, [str(audio_file), str(audio_file)])
            self.assertTrue(voice_client.is_playing())
            self.assertTrue(voice_client.is_recording())
            self.assertEqual(list(session.queue), [])

            # A delayed completion from the first attempt must not own the
            # resumed attempt, even though both attempts use the same item.
            first_after(None)
            await asyncio.sleep(0)
            self.assertIs(session.current_audio, first)
            self.assertTrue(voice_client.is_playing())
            self.assertTrue(audio_file.exists())

            voice_client.finish_playback()
            await asyncio.sleep(0)
            self.assertIsNone(session.current_audio)
            self.assertFalse(audio_file.exists())
            self.assertIsNotNone(resumed_after)
            await bot.invalidate_voice_session(GUILD_ID)

    async def test_start_and_abort_tts_playback_preserve_native_reader(self):
        bot = _bare_bot()
        voice_client = FakeVoiceClient()
        with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
            session = await bot.start_listening(voice_client, GUILD_ID)
        reader = voice_client._reader
        voice_client.playing = True

        self.assertTrue(
            bot.start_tts_stream(
                CHANNEL_ID,
                session.generation,
                "tts-one",
                48_000,
                2,
                2,
            )
        )
        self.assertIs(voice_client._reader, reader)
        self.assertTrue(voice_client.is_recording())
        self.assertTrue(bot.abort_tts_stream(CHANNEL_ID, "tts-one"))
        self.assertIs(voice_client._reader, reader)
        self.assertTrue(voice_client.is_recording())
        self.assertEqual(reader.stop_count, 0)
        await bot.invalidate_voice_session(GUILD_ID)

    async def test_canceling_queued_playback_removes_it_before_it_can_play_later(self):
        bot = _bare_bot()
        voice_client = FakeVoiceClient()
        session = _session(bot, voice_client)
        voice_client.playing = True
        audio_file = _audio_file("discord-canceled-")
        bot.own_temp_audio(str(audio_file))

        with patch("discord_client.discord.FFmpegPCMAudio", side_effect=lambda path, **_: path):
            waiting = asyncio.create_task(
                bot.speak(
                    CHANNEL_ID,
                    str(audio_file),
                    session.generation,
                    wait_until_started=True,
                )
            )
            await asyncio.sleep(0)
            self.assertEqual(len(session.queue), 1)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting

            self.assertEqual(list(session.queue), [])
            self.assertFalse(audio_file.exists())
            voice_client.playing = False
            bot._play_next(session)

        self.assertEqual(voice_client.played, [])
        await bot.invalidate_voice_session(GUILD_ID)

    async def test_session_invalidation_cleans_only_registered_owned_audio(self):
        bot = _bare_bot()
        voice_client = FakeVoiceClient()
        session = _session(bot, voice_client)
        with tempfile.TemporaryDirectory(prefix="discord-audio-ownership-") as folder:
            owned = Path(folder, "owned.wav")
            borrowed = Path(folder, "borrowed.wav")
            current = Path(folder, "current.wav")
            interrupted = Path(folder, "interrupted.wav")
            owned.write_bytes(b"adapter temp")
            borrowed.write_bytes(b"caller file")
            current.write_bytes(b"active adapter temp")
            interrupted.write_bytes(b"interrupted adapter temp")
            bot.own_temp_audio(str(owned))
            bot.own_temp_audio(str(current))
            bot.own_temp_audio(str(interrupted))
            receipt = asyncio.get_running_loop().create_future()
            session.queue.extend(
                [
                    AudioItem(str(owned), cleanup_owned=True, started=receipt),
                    AudioItem(str(borrowed), cleanup_owned=False),
                ]
            )
            session.current_audio = AudioItem(str(current), cleanup_owned=True)
            session.interrupted_audio = AudioItem(
                str(interrupted), cleanup_owned=True
            )

            await bot.invalidate_voice_session(GUILD_ID)

            self.assertFalse(owned.exists())
            self.assertFalse(current.exists())
            self.assertFalse(interrupted.exists())
            self.assertTrue(borrowed.exists())
            self.assertTrue(receipt.done())
            self.assertFalse(receipt.result())
            self.assertEqual(bot._owned_temp_audio, set())

    async def test_decoder_start_failure_returns_false_and_cleans_owned_audio(self):
        bot = _bare_bot()
        voice_client = FakeVoiceClient()
        voice_client.fail_play = True
        session = _session(bot, voice_client)
        audio_file = _audio_file("discord-play-fail-")
        bot.own_temp_audio(str(audio_file))

        with patch("discord_client.discord.FFmpegPCMAudio", side_effect=lambda path, **_: path):
            started = await bot.speak(
                CHANNEL_ID,
                str(audio_file),
                session.generation,
                wait_until_started=True,
            )

        self.assertFalse(started)
        self.assertFalse(audio_file.exists())
        self.assertEqual(list(session.queue), [])
        await bot.invalidate_voice_session(GUILD_ID)


if __name__ == "__main__":
    unittest.main()
