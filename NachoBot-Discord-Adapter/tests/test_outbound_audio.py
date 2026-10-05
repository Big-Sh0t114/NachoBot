import asyncio
import base64
import io
import json
import logging
import math
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import outbound_audio
import adapter as adapter_module
import test_native_transport as fixtures
from discord_client import resolve_ffmpeg_executable
from outbound_audio import (
    AudioProcessingError,
    MP3Attachment,
    VoiceMessageAudio,
    prepare_mp3_attachment,
    prepare_voice_message,
    send_ephemeral_mp3_followup,
    send_native_voice_message,
)


USER_ID = fixtures.USER_ID
CHANNEL_ID = fixtures.CHANNEL_ID
GUILD_ID = fixtures.GUILD_ID
MESSAGE_ID = fixtures.MESSAGE_ID


def make_wav(duration=0.8, *, rate=48_000, channels=2):
    samples = array_sine(duration, rate)
    pcm = bytearray()
    for sample in samples:
        for _ in range(channels):
            pcm.extend(int(sample).to_bytes(2, "little", signed=True))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


def array_sine(duration, rate):
    return [int(12_000 * math.sin(2 * math.pi * 440 * index / rate)) for index in range(int(duration * rate))]


def write_silence_wav(path: Path, duration: int) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(48_000)
        remaining = duration * 48_000 * 4
        zeroes = b"\0" * (64 * 1024)
        while remaining:
            chunk = zeroes[: min(len(zeroes), remaining)]
            wav_file.writeframesraw(chunk)
            remaining -= len(chunk)


class CapturingHTTP:
    def __init__(self, channel_id=CHANNEL_ID, message_id="823456789012345679"):
        self.channel_id = str(channel_id)
        self.message_id = str(message_id)
        self.calls = []

    async def request(self, route, **kwargs):
        form = kwargs["form"]
        fields = {item["name"]: item for item in form}
        payload = json.loads(fields["payload_json"]["value"])
        audio = fields["files[0]"]["value"].read()
        self.calls.append(
            {
                "route": route,
                "kwargs": kwargs,
                "payload": payload,
                "audio": audio,
                "content_type": fields["files[0]"].get("content_type"),
                "filename": fields["files[0]"]["filename"],
            }
        )
        return {"id": self.message_id, "channel_id": self.channel_id}


class _WaitingReader:
    def __init__(self, release):
        self.release = release

    async def read(self, _size):
        await self.release.wait()
        return b""


class _StuckProcess:
    def __init__(self):
        self.release = asyncio.Event()
        self.stdout = _WaitingReader(self.release)
        self.stdin = None
        self.returncode = None
        self.kill_calls = 0

    async def wait(self):
        await self.release.wait()
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self.release.set()


class OutboundAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_wav_becomes_mono_opus_ogg_with_playable_pcm_and_bounded_waveform(self):
        voice = await prepare_voice_message(
            make_wav(1.2), ffmpeg_executable=resolve_ffmpeg_executable(), require_wav=True
        )

        self.assertTrue(voice.data.startswith(b"OggS"))
        self.assertIn(b"OpusHead", voice.data[:256])
        self.assertAlmostEqual(voice.duration_secs, 1.2, delta=0.03)
        waveform = base64.b64decode(voice.waveform, validate=True)
        self.assertGreater(len(waveform), 0)
        self.assertLessEqual(len(waveform), 256)
        self.assertGreater(max(waveform), 0)

        process = await asyncio.create_subprocess_exec(
            resolve_ffmpeg_executable(),
            "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "wav,mp3,ogg,flac,mov,aac",
            "-i", "pipe:0", "-map", "0:a:0", "-ar", "48000", "-ac", "1",
            "-f", "s16le", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        decoded, _ = await process.communicate(voice.data)
        self.assertEqual(process.returncode, 0)
        self.assertGreater(len(decoded), 48_000 * 2)

    async def test_native_voice_multipart_has_flag_metadata_and_audio_mime(self):
        waveform = base64.b64encode(bytes((0, 40, 180, 255))).decode("ascii")
        audio = VoiceMessageAudio(b"OggS" + b"0" * 32 + b"OpusHead", "voice.ogg", 1.25, waveform)
        http = CapturingHTTP()

        receipt = await send_native_voice_message(http, CHANNEL_ID, audio)

        call = http.calls[0]
        self.assertEqual(call["route"].method, "POST")
        self.assertIn("/channels/" + CHANNEL_ID + "/messages", call["route"].url)
        self.assertEqual(call["content_type"], "audio/ogg")
        self.assertEqual(call["filename"], "voice.ogg")
        self.assertEqual(call["audio"], audio.data)
        self.assertEqual(call["payload"]["flags"], outbound_audio.VOICE_MESSAGE_FLAG)
        self.assertEqual(call["payload"]["attachments"][0]["duration_secs"], 1.25)
        self.assertEqual(call["payload"]["attachments"][0]["waveform"], waveform)
        self.assertEqual(receipt, {"message_id": "823456789012345679", "channel_id": CHANNEL_ID})

    async def test_text_tts_base64_wav_sends_native_voice_message_and_ack(self):
        channel = fixtures.FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = fixtures._adapter(
            config=fixtures._config(voice=fixtures.VoiceConfig(enabled=False, use_tts=True)),
            channels={CHANNEL_ID: channel},
        )
        http = CapturingHTTP()
        adapter.bot.http = http
        message = fixtures._core_message(
            target={
                "schema_version": 1,
                "transport": "discord",
                "channel_id": CHANNEL_ID,
                "user_id": USER_ID,
                "guild_id": GUILD_ID,
                "mode": "text",
                "voice_generation": "",
            },
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            segment={"type": "voice", "data": base64.b64encode(make_wav()).decode("ascii")},
        )

        await adapter.handle_from_nachobot(message)

        self.assertEqual(len(http.calls), 1)
        self.assertTrue(http.calls[0]["audio"].startswith(b"OggS"))
        self.assertEqual(http.calls[0]["content_type"], "audio/ogg")
        self.assertEqual(channel.sent, [])
        receipt = adapter.router.send_custom_message.await_args.args[2]
        self.assertEqual(receipt["actual_id"], "823456789012345679")

    async def test_recognized_voice_ingress_advertises_wav_payload_support(self):
        adapter = fixtures._adapter()
        adapter.bot._session_is_current = lambda _session: True
        session = SimpleNamespace(
            channel_id=int(CHANNEL_ID),
            guild_id=int(GUILD_ID),
            generation="voice-generation-1",
            voice_client=SimpleNamespace(
                channel=SimpleNamespace(name="voice"),
                guild=SimpleNamespace(get_member=lambda _user_id: SimpleNamespace(display_name="林")),
            ),
        )

        await adapter.handle_speech_recognized(
            session,
            int(USER_ID),
            "AQID",
            capture_id="capture-1",
            scope="capture-scope-1",
        )

        request = adapter.router.send_message.await_args.args[0]
        self.assertEqual(
            request.message_info.additional_config["runtime_capabilities"][
                "voice_payload_formats"
            ],
            ["wav"],
        )

    async def test_local_voicefile_over_twenty_mib_is_compressed_and_source_copy_is_cleaned(self):
        channel = fixtures.FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = fixtures._adapter(channels={CHANNEL_ID: channel})
        http = CapturingHTTP()
        adapter.bot.http = http
        copied = []
        copy_audio = adapter._copy_local_media_to_temp_async

        async def record_copy(source):
            result = await copy_audio(source)
            copied.append(Path(result))
            return result

        adapter._copy_local_media_to_temp_async = record_copy
        message = fixtures._core_message(
            target={
                "schema_version": 1,
                "transport": "discord",
                "channel_id": CHANNEL_ID,
                "user_id": USER_ID,
                "guild_id": GUILD_ID,
                "mode": "text",
                "voice_generation": "",
            },
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            segment={"type": "voicefile", "data": ""},
        )

        with tempfile.TemporaryDirectory(prefix="discord-audio-local-") as folder:
            root = Path(folder)
            music = root / "NachoBot" / "music"
            music.mkdir(parents=True)
            source = music / "long-song.wav"
            write_silence_wav(source, 110)
            self.assertGreater(source.stat().st_size, 20 * 1024 * 1024)
            message.message_segment["data"] = str(source)
            with patch("adapter._root_dir", root):
                await adapter.handle_from_nachobot(message)

            self.assertTrue(source.exists())
            self.assertEqual(len(copied), 1)
            self.assertFalse(copied[0].exists())

        self.assertEqual(len(http.calls), 1)
        self.assertLess(len(http.calls[0]["audio"]), 1024 * 1024)
        self.assertEqual(http.calls[0]["content_type"], "audio/ogg")
        self.assertEqual(adapter.router.send_custom_message.await_args.args[2]["actual_id"], "823456789012345679")

    async def test_invalid_and_silk_wav_audio_are_not_acknowledged(self):
        for bad_audio in (b"not a wave", b"#!SILK_V3\nraw encoded audio"):
            with self.subTest(prefix=bad_audio[:8]):
                channel = fixtures.FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
                adapter = fixtures._adapter(
                    config=fixtures._config(voice=fixtures.VoiceConfig(enabled=False, use_tts=True)),
                    channels={CHANNEL_ID: channel},
                )
                http = CapturingHTTP()
                adapter.bot.http = http
                message = fixtures._core_message(
                    target={
                        "schema_version": 1,
                        "transport": "discord",
                        "channel_id": CHANNEL_ID,
                        "user_id": USER_ID,
                        "guild_id": GUILD_ID,
                        "mode": "text",
                        "voice_generation": "",
                    },
                    group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
                    user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
                    segment={"type": "voice", "data": base64.b64encode(bad_audio).decode("ascii")},
                )

                await adapter.handle_from_nachobot(message)

                self.assertEqual(http.calls, [])
                adapter.router.send_custom_message.assert_not_awaited()

    async def test_expired_slash_binding_does_not_send_custom_mp3_request(self):
        channel = fixtures.FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = fixtures._adapter(channels={CHANNEL_ID: channel})
        http = CapturingHTTP()
        followup = SimpleNamespace(id=int(MESSAGE_ID), token="private-test-token")
        target = adapter._target(CHANNEL_ID, USER_ID, GUILD_ID, "text")
        binding = adapter._slash_interactions.register(followup, target)
        target["interaction_key"] = binding.key
        adapter._slash_interactions.discard(binding.key)
        wrapper = adapter_module._InteractionFollowupDestination(
            binding, channel, adapter._slash_interactions, target, http
        )

        with self.assertRaises(RuntimeError):
            await wrapper.send_mp3(MP3Attachment(b"ID3fake"))
        self.assertEqual(http.calls, [])

    async def test_slash_voice_result_is_ephemeral_playable_mp3_without_voice_flag(self):
        channel = fixtures.FakeChannel(CHANNEL_ID, guild=SimpleNamespace(id=int(GUILD_ID)))
        adapter = fixtures._adapter(
            config=fixtures._config(voice=fixtures.VoiceConfig(enabled=False, use_tts=True)),
            channels={CHANNEL_ID: channel},
        )
        http = CapturingHTTP()
        adapter.bot.http = http
        followup = fixtures.FakeInteractionFollowup()
        followup.id = int(MESSAGE_ID)
        followup.token = "private-test-token"
        target = adapter._target(CHANNEL_ID, USER_ID, GUILD_ID, "text")
        binding = adapter._slash_interactions.register(followup, target)
        target["interaction_key"] = binding.key
        message = fixtures._core_message(
            target=target,
            group=SimpleNamespace(platform="discord", group_id="legacy-room-8"),
            user=SimpleNamespace(platform="discord", user_id="legacy-user-41"),
            segment={"type": "voice", "data": base64.b64encode(make_wav()).decode("ascii")},
        )

        await adapter.handle_from_nachobot(message)

        self.assertEqual(followup.sent, [])
        self.assertEqual(channel.sent, [])
        self.assertEqual(len(http.calls), 1)
        call = http.calls[0]
        self.assertIn("/webhooks/", call["route"].url)
        self.assertEqual(call["kwargs"]["params"], {"wait": "true"})
        self.assertEqual(call["content_type"], "audio/mpeg")
        self.assertTrue(call["filename"].endswith(".mp3"))
        self.assertTrue(call["audio"].startswith(b"ID3") or call["audio"][:1] == b"\xff")
        self.assertEqual(call["payload"]["flags"], outbound_audio.EPHEMERAL_FLAG)
        self.assertNotEqual(call["payload"]["flags"] & outbound_audio.VOICE_MESSAGE_FLAG, outbound_audio.VOICE_MESSAGE_FLAG)
        self.assertEqual(
            adapter.router.send_custom_message.await_args.args[2]["actual_id"],
            "823456789012345679",
        )

    async def test_private_followup_redacts_webhook_and_bot_tokens_from_sdk_logs(self):
        http = CapturingHTTP()
        http.token = "test-bot-auth-secret"
        logger = logging.getLogger("discord.http")
        previous_level = logger.level
        records = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        class LoggingHTTP(CapturingHTTP):
            async def request(self, route, **kwargs):
                logging.getLogger("discord.http").debug(
                    "POST %s Authorization: Bot %s", route.url, http.token
                )
                return await super().request(route, **kwargs)

        http = LoggingHTTP()
        http.token = "test-bot-auth-secret"
        webhook_token = "test-webhook-secret"
        handler = CaptureHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            await send_ephemeral_mp3_followup(
                http,
                MESSAGE_ID,
                webhook_token,
                CHANNEL_ID,
                MP3Attachment(b"ID3fixture"),
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        self.assertIn(webhook_token, http.calls[0]["route"].url)
        self.assertTrue(records)
        self.assertTrue(all(webhook_token not in record for record in records))
        self.assertTrue(all(http.token not in record for record in records))
        self.assertIn("[redacted]", records[0])

    async def test_disguised_playlist_is_rejected_without_http_fetch(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"not audio")

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="discord-audio-playlist-") as folder:
                playlist = Path(folder) / "disguised.mp3"
                playlist.write_text(
                    f"#EXTM3U\nhttp://127.0.0.1:{server.server_port}/payload.ts\n",
                    encoding="utf-8",
                )
                with self.assertRaises(AudioProcessingError):
                    await prepare_voice_message(
                        playlist, ffmpeg_executable=resolve_ffmpeg_executable()
                    )
            self.assertEqual(requests, [])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    async def test_cancellation_kills_and_drains_ffmpeg_process(self):
        with tempfile.TemporaryDirectory(prefix="discord-audio-cancel-") as folder:
            source = Path(folder) / "audio.wav"
            source.write_bytes(make_wav())
            process = _StuckProcess()
            started = asyncio.Event()

            async def spawn(*_args, **_kwargs):
                started.set()
                return process

            existing_tasks = set(asyncio.all_tasks())
            with patch.object(outbound_audio.asyncio, "create_subprocess_exec", side_effect=spawn):
                task = asyncio.create_task(
                    prepare_voice_message(source, ffmpeg_executable="fake-ffmpeg", require_wav=True)
                )
                await asyncio.wait_for(started.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
            self.assertGreaterEqual(process.kill_calls, 1)
            self.assertIsNotNone(process.returncode)
            self.assertFalse(
                [task for task in asyncio.all_tasks() - existing_tasks if not task.done()]
            )

    async def test_timeout_kills_ffmpeg_without_unbounded_wait(self):
        with tempfile.TemporaryDirectory(prefix="discord-audio-timeout-") as folder:
            source = Path(folder) / "audio.wav"
            source.write_bytes(make_wav())
            process = _StuckProcess()
            existing_tasks = set(asyncio.all_tasks())
            with patch.object(outbound_audio.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                with patch.object(outbound_audio, "FFMPEG_TIMEOUT_SECONDS", 0.01):
                    with self.assertRaises(AudioProcessingError):
                        await asyncio.wait_for(
                            prepare_voice_message(source, ffmpeg_executable="fake-ffmpeg", require_wav=True),
                            timeout=2,
                        )
            self.assertGreaterEqual(process.kill_calls, 1)
            self.assertIsNotNone(process.returncode)
            self.assertFalse(
                [task for task in asyncio.all_tasks() - existing_tasks if not task.done()]
            )


if __name__ == "__main__":
    unittest.main()
