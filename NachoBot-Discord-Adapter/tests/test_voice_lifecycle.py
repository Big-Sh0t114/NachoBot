from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.voice.state import VoiceConnectionState

import main as main_module
from discord_client import (
    NachoDiscordBot,
    RetryAwareVoiceClient,
    RetryAwareVoiceConnectionState,
    VoiceLifecycleError,
)

GUILD_ID = 771
CHANNEL_ID = 772
BOT_ID = 773


class FakeSink:
    def __init__(self, **callbacks):
        self.callbacks = callbacks
        self.closed = False
        self.close_count = 0

    async def aclose(self):
        self.close_count += 1
        self.closed = True


class FakeCoreStreams:
    async def abort(self, _stream_id):
        return None

    async def abort_all(self):
        return None


class FakeVoiceClient:
    def __init__(self, guild, channel, *, connected=True):
        self.guild = guild
        self.channel = channel
        self.connected = connected
        self.ws = object()
        self.session_id = "voice-session-1"
        self.recordings = []
        self.recording = False
        self.disconnect_count = 0
        self.playing = False
        self.paused = False
        self.source = None

    def is_connected(self):
        return self.connected

    def is_recording(self):
        return self.recording

    def start_recording(self, sink, callback):
        self.recordings.append((sink, callback))
        self.recording = True

    def stop_recording(self):
        self.recording = False

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    def stop(self):
        self.playing = False
        self.paused = False

    async def disconnect(self, *, force=False):
        self.disconnect_count += 1
        self.connected = False
        self.recording = False
        if self.guild.voice_client is self:
            self.guild.voice_client = None


def _channel(channel_id: int, name: str):
    return SimpleNamespace(id=channel_id, name=name, members=[])


def _bot_and_guild():
    bot = NachoDiscordBot.__new__(NachoDiscordBot)
    bot.adapter_config = SimpleNamespace(
        voice=SimpleNamespace(enabled=True, sample_rate=48_000)
    )
    bot.voice_handler = SimpleNamespace()
    bot.logger = Mock(spec=logging.Logger)
    bot.transport_adapter = None
    bot.speech_callback = None
    bot.voice_sessions = {}
    bot.tts_streams = {}
    bot._active_sinks = set()
    bot._owned_temp_audio = set()
    bot.core_audio_streams = FakeCoreStreams()
    bot.core_audio_stream_client = SimpleNamespace(close=AsyncMock())
    guild = SimpleNamespace(id=GUILD_ID, voice_client=None)
    bot.get_guild = Mock(return_value=guild)
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=BOT_ID))
    return bot, guild


async def _wait_for(predicate, *, timeout: float = 1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.005)


async def _sdk_cleanup_false(bot, voice_client):
    state = object.__new__(RetryAwareVoiceConnectionState)
    state.client = voice_client
    marker_seen_before_sdk_cleanup = []

    async def sdk_parent_disconnect(_state, **_kwargs):
        marker_seen_before_sdk_cleanup.append(
            bool(bot._voice_transient_disconnects.get(GUILD_ID))
        )

    with patch.object(VoiceConnectionState, "disconnect", new=sdk_parent_disconnect):
        await RetryAwareVoiceConnectionState.disconnect(state, cleanup=False)
    marker = bot._voice_transient_disconnects[GUILD_ID][-1]
    return marker_seen_before_sdk_cleanup, marker


class DiscordVoiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_aware_factory_preserves_the_installed_receive_hook(self):
        bot, _guild = _bot_and_guild()
        hook = Mock()
        channel = _channel(CHANNEL_ID, "voice")
        sdk_client = SimpleNamespace(
            _nacho_lifecycle_bot=bot,
            _recv_hook=hook,
            _state=SimpleNamespace(self_id=BOT_ID),
            channel=channel,
            channel_id=CHANNEL_ID,
            loop=asyncio.get_running_loop(),
        )

        with patch("discord.voice.state.SocketEventReader.start"):
            connection = RetryAwareVoiceClient.create_connection_state(sdk_client)

        self.assertIsInstance(connection, RetryAwareVoiceConnectionState)
        self.assertIs(connection.hook, hook)

    async def test_initial_cleanup_false_removal_does_not_cancel_retry_join(self):
        bot, guild = _bot_and_guild()
        channel = _channel(CHANNEL_ID, "voice")
        voice_client = FakeVoiceClient(guild, channel)
        member = SimpleNamespace(id=BOT_ID, guild=guild)
        marker_seen_before_sdk_cleanup = []
        passed_client_class = []

        async def connect(**kwargs):
            passed_client_class.append(kwargs.get("cls"))
            guild.voice_client = voice_client
            voice_client._nacho_lifecycle_bot = bot
            self.assertTrue(bot._voice_lock(GUILD_ID).locked())
            marker_seen, marker = await _sdk_cleanup_false(bot, voice_client)
            marker_seen_before_sdk_cleanup.extend(marker_seen)
            self.assertEqual(
                marker.attempt_id,
                bot._voice_connect_attempts[GUILD_ID].attempt_id,
            )
            await asyncio.wait_for(
                bot.on_voice_state_update(
                    member,
                    SimpleNamespace(channel=channel),
                    SimpleNamespace(channel=None),
                ),
                timeout=0.05,
            )
            voice_client.connected = True
            return voice_client

        channel.connect = connect
        with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
            self.assertEqual(
                await bot.join_voice_channel(guild, channel), ("joined", None)
            )
            self.assertEqual(passed_client_class, [RetryAwareVoiceClient])
            self.assertEqual(marker_seen_before_sdk_cleanup, [True])
            self.assertEqual(len(voice_client.recordings), 1)
            self.assertIn(GUILD_ID, bot._voice_recovery_intent)
            await bot.leave_voice_channel(guild)

    async def test_concurrent_joins_share_one_connect_and_one_receive_session(self):
        bot, guild = _bot_and_guild()
        channel = _channel(CHANNEL_ID, "voice")
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def connect(**_kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            voice_client = FakeVoiceClient(guild, channel)
            guild.voice_client = voice_client
            return voice_client

        channel.connect = connect
        with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
            first = asyncio.create_task(bot.join_voice_channel(guild, channel))
            await entered.wait()
            second = asyncio.create_task(bot.join_voice_channel(guild, channel))
            await asyncio.sleep(0.02)
            self.assertEqual(calls, 1)
            release.set()
            results = await asyncio.gather(first, second)

            self.assertEqual({result[0] for result in results}, {"joined", "already"})
            self.assertEqual(calls, 1)
            self.assertEqual(len(guild.voice_client.recordings), 1)
            await bot.leave_voice_channel(guild)

    async def test_disconnected_client_never_starts_recording(self):
        bot, guild = _bot_and_guild()
        channel = _channel(CHANNEL_ID, "voice")
        voice_client = FakeVoiceClient(guild, channel, connected=False)
        guild.voice_client = voice_client

        with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
            session = await bot.start_listening(voice_client, GUILD_ID)

        self.assertIsNone(session)
        self.assertFalse(voice_client.recordings)
        self.assertNotIn(GUILD_ID, bot.voice_sessions)

    async def test_internal_voice_reconnect_waits_then_starts_one_fresh_sink(self):
        bot, guild = _bot_and_guild()
        channel = _channel(CHANNEL_ID, "voice")
        voice_client = FakeVoiceClient(guild, channel)

        async def connect(**_kwargs):
            voice_client.connected = True
            guild.voice_client = voice_client
            voice_client._nacho_lifecycle_bot = bot
            return voice_client

        channel.connect = connect
        with (
            patch("discord_client.SilenceDetectingSink", side_effect=FakeSink),
            patch("discord_client.VOICE_RECOVERY_POLL_INTERVAL", 0.005),
            patch("discord_client.VOICE_CONNECT_TIMEOUT", 0.5),
        ):
            await bot.join_voice_channel(guild, channel)
            old_session = bot.voice_sessions[GUILD_ID]
            old_sink = old_session.sink
            voice_client.connected = False
            voice_client.ws = object()
            marker_seen, marker = await _sdk_cleanup_false(bot, voice_client)
            self.assertIs(marker.voice_client, voice_client)
            self.assertEqual(marker.channel_id, channel.id)
            member = SimpleNamespace(id=BOT_ID, guild=guild)
            await bot.on_voice_state_update(
                member,
                SimpleNamespace(channel=channel),
                SimpleNamespace(channel=None),
            )
            self.assertEqual(marker_seen, [True])
            self.assertIn(GUILD_ID, bot._voice_recovery_intent)
            await _wait_for(lambda: not voice_client.recording)
            voice_client.session_id = "voice-session-2"
            voice_client.connected = True
            await _wait_for(lambda: len(voice_client.recordings) == 2)

            new_session = bot.voice_sessions[GUILD_ID]
            self.assertIsNot(new_session, old_session)
            self.assertNotEqual(new_session.generation, old_session.generation)
            self.assertEqual(new_session.voice_session_id, "voice-session-2")
            self.assertTrue(old_sink.closed)
            await asyncio.sleep(0.03)
            self.assertEqual(len(voice_client.recordings), 2)
            await bot.leave_voice_channel(guild)

    async def test_move_waits_for_voice_client_to_become_ready(self):
        bot, guild = _bot_and_guild()
        old_channel = _channel(CHANNEL_ID, "old")
        new_channel = _channel(CHANNEL_ID + 1, "new")
        voice_client = FakeVoiceClient(guild, old_channel)
        guild.voice_client = voice_client

        async def move_to(target):
            voice_client.channel = target
            voice_client.connected = False

            async def finish_move():
                await asyncio.sleep(0.03)
                voice_client.ws = object()
                voice_client.session_id = "voice-session-moved"
                voice_client.connected = True

            asyncio.create_task(finish_move())

        voice_client.move_to = move_to
        with (
            patch("discord_client.SilenceDetectingSink", side_effect=FakeSink),
            patch("discord_client.VOICE_CONNECT_TIMEOUT", 0.5),
        ):
            await bot.join_voice_channel(guild, old_channel)
            result = await bot.join_voice_channel(guild, new_channel)

            self.assertEqual(result, ("joined", None))
            self.assertEqual(voice_client.channel, new_channel)
            self.assertTrue(voice_client.connected)
            self.assertEqual(len(voice_client.recordings), 2)
            self.assertEqual(bot.voice_sessions[GUILD_ID].channel_id, new_channel.id)
            await bot.leave_voice_channel(guild)

    async def test_failed_connect_is_cleaned_and_a_later_explicit_join_succeeds(self):
        bot, guild = _bot_and_guild()
        channel = _channel(CHANNEL_ID, "voice")
        failed_client = FakeVoiceClient(guild, channel, connected=False)
        calls = 0

        async def connect(**_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                guild.voice_client = failed_client
                raise TimeoutError()
            client = FakeVoiceClient(guild, channel)
            guild.voice_client = client
            return client

        channel.connect = connect
        with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
            with self.assertRaises(VoiceLifecycleError):
                await bot.join_voice_channel(guild, channel)
            self.assertIsNone(guild.voice_client)
            self.assertEqual(failed_client.disconnect_count, 1)
            self.assertFalse(bot._voice_recovery_intent)
            self.assertFalse(bot._voice_recovery_tasks)

            result = await bot.join_voice_channel(guild, channel)
            self.assertEqual(result[0], "joined")
            self.assertEqual(calls, 2)
            self.assertEqual(len(guild.voice_client.recordings), 1)
            await bot.leave_voice_channel(guild)

    async def test_canceled_initial_join_cleans_connector_without_phantom_recovery(self):
        bot, guild = _bot_and_guild()
        channel = _channel(CHANNEL_ID, "voice")
        entered = asyncio.Event()
        never_release = asyncio.Event()
        candidate = FakeVoiceClient(guild, channel, connected=False)

        async def connect(**_kwargs):
            guild.voice_client = candidate
            entered.set()
            await never_release.wait()
            return candidate

        channel.connect = connect
        with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
            joining = asyncio.create_task(bot.join_voice_channel(guild, channel))
            await entered.wait()
            joining.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await joining

        self.assertIsNone(guild.voice_client)
        self.assertEqual(candidate.disconnect_count, 1)
        self.assertFalse(bot._voice_recovery_intent)
        self.assertFalse(bot._voice_recovery_tasks)

    async def test_canceled_move_disconnects_client_and_clears_recovery_intent(self):
        bot, guild = _bot_and_guild()
        old_channel = _channel(CHANNEL_ID, "old")
        new_channel = _channel(CHANNEL_ID + 1, "new")
        voice_client = FakeVoiceClient(guild, old_channel)
        guild.voice_client = voice_client
        move_entered = asyncio.Event()

        async def move_to(target):
            voice_client.channel = target
            move_entered.set()
            await asyncio.Event().wait()

        voice_client.move_to = move_to
        with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
            await bot.join_voice_channel(guild, old_channel)
            moving = asyncio.create_task(bot.join_voice_channel(guild, new_channel))
            await move_entered.wait()
            moving.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await moving

        self.assertIsNone(guild.voice_client)
        self.assertEqual(voice_client.disconnect_count, 1)
        self.assertFalse(bot._voice_recovery_intent)
        self.assertFalse(bot._voice_recovery_tasks)

    async def test_leave_waiting_for_join_clears_the_new_intent_and_task(self):
        bot, guild = _bot_and_guild()
        channel = _channel(CHANNEL_ID, "voice")
        entered = asyncio.Event()
        release = asyncio.Event()
        voice_client = FakeVoiceClient(guild, channel)

        async def connect(**_kwargs):
            entered.set()
            await release.wait()
            guild.voice_client = voice_client
            return voice_client

        channel.connect = connect
        with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
            joining = asyncio.create_task(bot.join_voice_channel(guild, channel))
            await entered.wait()
            leaving = asyncio.create_task(bot.leave_voice_channel(guild))
            await asyncio.sleep(0.02)
            release.set()
            self.assertEqual((await joining)[0], "joined")
            await leaving

        self.assertIsNone(guild.voice_client)
        self.assertFalse(bot._voice_recovery_intent)
        self.assertFalse(bot._voice_recovery_tasks)
        self.assertEqual(voice_client.disconnect_count, 1)

    async def test_new_client_kick_is_not_hidden_by_older_leave_cleanup(self):
        bot, guild = _bot_and_guild()
        channel = _channel(CHANNEL_ID, "voice")
        old_client = FakeVoiceClient(guild, channel)
        new_client = FakeVoiceClient(guild, channel)
        clients = iter((old_client, new_client))

        async def connect(**_kwargs):
            voice_client = next(clients)
            voice_client.connected = True
            guild.voice_client = voice_client
            return voice_client

        channel.connect = connect
        with patch("discord_client.SilenceDetectingSink", side_effect=FakeSink):
            await bot.join_voice_channel(guild, channel)
            old_client._nacho_lifecycle_bot = bot
            bot._mark_transient_voice_disconnect(old_client)
            await bot.leave_voice_channel(guild)
            await bot.join_voice_channel(guild, channel)
            self.assertIs(guild.voice_client, new_client)

            # A delayed Gateway removal for the new client must be recognized
            # as a real kick, even while the old leave marker is still fresh.
            new_client.connected = False
            member = SimpleNamespace(id=BOT_ID, guild=guild)
            await bot.on_voice_state_update(
                member,
                SimpleNamespace(channel=channel),
                SimpleNamespace(channel=None),
            )

        self.assertFalse(bot._voice_recovery_intent)
        self.assertFalse(bot._voice_recovery_tasks)
        self.assertIsNone(guild.voice_client)
        self.assertEqual(new_client.disconnect_count, 1)

    async def test_kick_gateway_resume_and_shutdown_control_recovery(self):
        bot, guild = _bot_and_guild()
        channel = _channel(CHANNEL_ID, "voice")
        voice_client = FakeVoiceClient(guild, channel)

        async def connect(**_kwargs):
            voice_client.connected = True
            guild.voice_client = voice_client
            return voice_client

        channel.connect = connect
        with (
            patch("discord_client.SilenceDetectingSink", side_effect=FakeSink),
            patch("discord_client.VOICE_RECOVERY_POLL_INTERVAL", 0.005),
            patch("discord_client.VOICE_CONNECT_TIMEOUT", 0.5),
            patch.object(discord.Bot, "close", new=AsyncMock()),
        ):
            await bot.join_voice_channel(guild, channel)
            await bot.on_disconnect()
            self.assertIn(GUILD_ID, bot._voice_recovery_intent)
            self.assertNotIn(GUILD_ID, bot._voice_recovery_tasks)
            voice_client.connected = True
            voice_client.ws = object()
            await bot.on_resumed()
            await _wait_for(lambda: len(voice_client.recordings) == 2)

            before = SimpleNamespace(channel=channel)
            after = SimpleNamespace(channel=None)
            member = SimpleNamespace(id=BOT_ID, guild=guild)
            await bot.on_voice_state_update(member, before, after)
            self.assertFalse(bot._voice_recovery_intent)
            self.assertFalse(bot._voice_recovery_tasks)
            self.assertIsNone(guild.voice_client)

            # A fresh explicit join can still succeed after the kick.
            await bot.join_voice_channel(guild, channel)
            self.assertTrue(bot._voice_recovery_tasks)
            await bot.close()
            self.assertFalse(bot._voice_recovery_intent)
            self.assertFalse(bot._voice_recovery_tasks)
            self.assertIsNone(guild.voice_client)

    def test_crypto_errors_keep_first_record_and_emit_bounded_repeat_count(self):
        rate_limit = main_module.CryptoErrorRateLimitFilter(interval=10)
        first = logging.LogRecord(
            "discord.voice.receive.reader",
            logging.ERROR,
            __file__,
            1,
            "CryptoError: invalid packet",
            (),
            None,
        )
        repeated = logging.LogRecord(
            "discord.voice.receive.reader",
            logging.ERROR,
            __file__,
            1,
            "CryptoError: invalid packet",
            (),
            None,
        )
        summary = logging.LogRecord(
            "discord.voice.receive.reader",
            logging.ERROR,
            __file__,
            1,
            "CryptoError: invalid packet",
            (),
            None,
        )
        with patch.object(
            main_module.time, "monotonic", side_effect=[0.0, 1.0, 10.0]
        ):
            self.assertTrue(rate_limit.filter(first))
            self.assertFalse(rate_limit.filter(repeated))
            self.assertTrue(rate_limit.filter(summary))

        self.assertIn("CryptoError", first.getMessage())
        self.assertIn("repeats since previous report: 2", summary.getMessage())


if __name__ == "__main__":
    unittest.main()
