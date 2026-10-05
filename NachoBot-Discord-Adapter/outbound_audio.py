"""Bounded audio conversion and Discord voice-message multipart delivery."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import os
import stat
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import discord
from discord.http import Route


MAX_AUDIO_INPUT_BYTES = 128 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 15 * 60
MAX_AUDIO_PCM_BYTES = 48_000 * 2 * MAX_AUDIO_DURATION_SECONDS
MAX_AUDIO_OUTPUT_BYTES = 25 * 1024 * 1024
MAX_WAVEFORM_POINTS = 256
MIN_AUDIO_DURATION_SECONDS = 0.1
FFMPEG_TIMEOUT_SECONDS = 90.0
VOICE_MESSAGE_FLAG = 1 << 13
EPHEMERAL_FLAG = 1 << 6
_WAVEFORM_BUCKET_SAMPLES = 4_800  # Discord clients sample voice at most every 100 ms.
_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
_ACTIVE_HTTP_LOG_SECRETS: ContextVar[tuple[str, ...]] = ContextVar(
    "discord_audio_http_log_secrets", default=()
)


class _DiscordHTTPSecretFilter(logging.Filter):
    """Redact credentials only from SDK log records in the active request task."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = _ACTIVE_HTTP_LOG_SECRETS.get()
        if secrets:
            message = record.getMessage()
            for secret in secrets:
                message = message.replace(secret, "[redacted]")
            record.msg = message
            record.args = ()
        return True


_DISCORD_HTTP_LOG_FILTER = _DiscordHTTPSecretFilter()
_DISCORD_HTTP_LOGGER = logging.getLogger("discord.http")
if not any(isinstance(item, _DiscordHTTPSecretFilter) for item in _DISCORD_HTTP_LOGGER.filters):
    _DISCORD_HTTP_LOGGER.addFilter(_DISCORD_HTTP_LOG_FILTER)


@contextmanager
def _redact_http_secrets(*values: Any):
    secrets = set(_ACTIVE_HTTP_LOG_SECRETS.get())
    for value in values:
        if isinstance(value, str) and value:
            secrets.add(value)
            secrets.add(quote(value, safe=""))
    token = _ACTIVE_HTTP_LOG_SECRETS.set(tuple(secrets))
    try:
        yield
    finally:
        _ACTIVE_HTTP_LOG_SECRETS.reset(token)


def _http_client_token(http_client: Any) -> str | None:
    token = getattr(http_client, "token", None)
    return token if isinstance(token, str) and token else None


class AudioProcessingError(ValueError):
    """Raised when audio is invalid or exceeds a configured resource bound."""


@dataclass(frozen=True)
class AudioProfile:
    duration_secs: float
    waveform: str


@dataclass(frozen=True)
class VoiceMessageAudio:
    data: bytes
    filename: str
    duration_secs: float
    waveform: str


@dataclass(frozen=True)
class MP3Attachment:
    data: bytes
    filename: str = "nachobot-audio.mp3"


AudioSource = bytes | bytearray | memoryview | Path | str


def _source_info(source: AudioSource, *, require_wav: bool = False) -> tuple[str | None, bytes | None, os.stat_result | None]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        if not data or len(data) > MAX_AUDIO_INPUT_BYTES:
            raise AudioProcessingError("audio input is empty or exceeds its size bound")
        _validate_audio_header(data[:16], suffix=".wav" if require_wav else "", require_wav=require_wav)
        return None, data, None

    path = Path(source)
    try:
        info = path.stat()
    except OSError as exc:
        raise AudioProcessingError("audio source is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > MAX_AUDIO_INPUT_BYTES:
        raise AudioProcessingError("audio source is not a bounded regular file")
    suffix = path.suffix.lower()
    if suffix == ".silk" or (suffix and suffix not in _AUDIO_SUFFIXES):
        raise AudioProcessingError("unsupported audio source format")
    try:
        with path.open("rb") as source_file:
            header = source_file.read(16)
    except OSError as exc:
        raise AudioProcessingError("audio source is unavailable") from exc
    _validate_audio_header(header, suffix=suffix, require_wav=require_wav)
    return str(path), None, info


def _validate_audio_header(header: bytes, *, suffix: str, require_wav: bool) -> None:
    if header.startswith(b"#!SILK_V3") or suffix == ".silk":
        raise AudioProcessingError("SILK is not a supported Discord audio attachment")
    if require_wav or suffix == ".wav":
        if len(header) < 12 or header[:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE":
            raise AudioProcessingError("WAV audio must contain a valid RIFF/WAVE header")


def _input_args(input_name: str) -> tuple[str, ...]:
    # Keep FFmpeg from following network protocols or accepting nested playlist,
    # concat, script, and other non-audio demuxers from an authorized media path.
    return (
        "-protocol_whitelist", "file,pipe",
        "-format_whitelist", "wav,mp3,ogg,flac,mov,aac",
        "-i", input_name,
    )


async def _run_ffmpeg(
    executable: str,
    source: AudioSource,
    args_factory,
    *,
    output_limit: int,
    timeout: float,
    on_chunk=None,
) -> bytes:
    input_path, input_bytes, initial_stat = _source_info(source)
    input_name = "pipe:0" if input_bytes is not None else input_path
    assert input_name is not None
    args = args_factory(input_name)
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-nostdin", "-hide_banner", "-loglevel", "error",
            *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, RuntimeError) as exc:
        raise AudioProcessingError("FFmpeg could not be started") from exc

    output = bytearray()
    output_size = 0

    async def write_input() -> None:
        if input_bytes is None:
            return
        assert process.stdin is not None
        try:
            for offset in range(0, len(input_bytes), 64 * 1024):
                process.stdin.write(input_bytes[offset : offset + 64 * 1024])
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # FFmpeg's non-zero status below carries the useful failure signal.
            pass
        finally:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.stdin.wait_closed(), timeout=1.0)
                except (BrokenPipeError, ConnectionResetError, OSError, asyncio.TimeoutError):
                    pass

    async def read_output() -> None:
        nonlocal output_size
        assert process.stdout is not None
        while True:
            chunk = await process.stdout.read(64 * 1024)
            if not chunk:
                return
            if output_size + len(chunk) > output_limit:
                raise AudioProcessingError("FFmpeg output exceeded its size bound")
            output_size += len(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
            else:
                output.extend(chunk)

    tasks = [asyncio.create_task(read_output()), asyncio.create_task(process.wait())]
    if input_bytes is not None:
        tasks.append(asyncio.create_task(write_input()))
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
        if process.returncode != 0:
            raise AudioProcessingError("FFmpeg could not decode the audio source")
        if initial_stat is not None:
            try:
                final_stat = Path(input_path).stat()
            except OSError as exc:
                raise AudioProcessingError("audio source changed during conversion") from exc
            if (initial_stat.st_size, initial_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
                raise AudioProcessingError("audio source changed during conversion")
        return bytes(output)
    except asyncio.TimeoutError as exc:
        raise AudioProcessingError("FFmpeg audio processing timed out") from exc
    finally:
        async def cleanup() -> None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            cleanup_waiter = asyncio.create_task(process.wait())
            try:
                await asyncio.wait_for(cleanup_waiter, timeout=5.0)
            except asyncio.TimeoutError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                if not cleanup_waiter.done():
                    cleanup_waiter.cancel()
                await asyncio.wait({cleanup_waiter}, timeout=0.25)
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=1.0)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.wait(pending, timeout=0.25)
            if not cleanup_waiter.done():
                cleanup_waiter.cancel()
                await asyncio.wait({cleanup_waiter}, timeout=0.25)

        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=6.0)
            except (asyncio.TimeoutError, Exception):
                if not cleanup_task.done():
                    cleanup_task.cancel()
                await asyncio.gather(cleanup_task, return_exceptions=True)
            raise


async def _profile_audio(source: AudioSource, executable: str) -> AudioProfile:
    bins: list[int] = []
    samples_seen = 0
    bucket_samples = 0
    bucket_peak = 0
    pending_byte = b""

    def collect(chunk: bytes) -> None:
        nonlocal samples_seen, bucket_samples, bucket_peak, pending_byte
        pcm = pending_byte + chunk
        usable = len(pcm) & ~1
        pending_byte = pcm[usable:]
        values = array("h")
        values.frombytes(pcm[:usable])
        if sys.byteorder != "little":
            values.byteswap()
        for sample in values:
            bucket_peak = max(bucket_peak, abs(sample))
            bucket_samples += 1
            samples_seen += 1
            if bucket_samples == _WAVEFORM_BUCKET_SAMPLES:
                bins.append(min(255, round(bucket_peak * 255 / 32768)))
                bucket_samples = 0
                bucket_peak = 0

    await _run_ffmpeg(
        executable,
        source,
        lambda name: (
            *_input_args(name), "-map", "0:a:0", "-vn", "-map_metadata", "-1",
            "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "1", "-f", "s16le", "pipe:1",
        ),
        output_limit=MAX_AUDIO_PCM_BYTES,
        timeout=FFMPEG_TIMEOUT_SECONDS,
        on_chunk=collect,
    )
    if pending_byte or bucket_samples and samples_seen == 0:
        raise AudioProcessingError("FFmpeg returned incomplete PCM audio")
    if bucket_samples:
        bins.append(min(255, round(bucket_peak * 255 / 32768)))
    duration = samples_seen / 48_000
    if not math.isfinite(duration) or duration < MIN_AUDIO_DURATION_SECONDS or duration > MAX_AUDIO_DURATION_SECONDS:
        raise AudioProcessingError("audio duration is empty or exceeds its bound")
    if not bins:
        raise AudioProcessingError("audio has no waveform samples")
    if len(bins) > MAX_WAVEFORM_POINTS:
        total = len(bins)
        bins = [
            max(bins[(index * total) // MAX_WAVEFORM_POINTS : ((index + 1) * total) // MAX_WAVEFORM_POINTS])
            for index in range(MAX_WAVEFORM_POINTS)
        ]
    waveform = base64.b64encode(bytes(bins)).decode("ascii")
    return AudioProfile(duration_secs=round(duration, 3), waveform=waveform)


async def prepare_voice_message(
    source: AudioSource,
    *,
    ffmpeg_executable: str,
    max_input_bytes: int = MAX_AUDIO_INPUT_BYTES,
    max_output_bytes: int = MAX_AUDIO_OUTPUT_BYTES,
    require_wav: bool = False,
) -> VoiceMessageAudio:
    """Decode bounded audio and encode Discord's mono Opus-in-OGG voice format."""

    if isinstance(source, (bytes, bytearray, memoryview)):
        if len(source) > max_input_bytes:
            raise AudioProcessingError("audio input exceeds its configured bound")
    else:
        try:
            if Path(source).stat().st_size > max_input_bytes:
                raise AudioProcessingError("audio input exceeds its configured bound")
        except OSError as exc:
            raise AudioProcessingError("audio source is unavailable") from exc
    _source_info(source, require_wav=require_wav)
    profile = await _profile_audio(source, ffmpeg_executable)
    ogg = await _run_ffmpeg(
        ffmpeg_executable,
        source,
        lambda name: (
            *_input_args(name), "-map", "0:a:0", "-vn", "-map_metadata", "-1",
            "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k",
            "-vbr", "off", "-application", "voip", "-frame_duration", "20", "-f", "ogg", "pipe:1",
        ),
        output_limit=min(max_output_bytes, MAX_AUDIO_OUTPUT_BYTES),
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    if not ogg.startswith(b"OggS") or b"OpusHead" not in ogg[:256]:
        raise AudioProcessingError("FFmpeg output is not an OGG/Opus stream")
    return VoiceMessageAudio(
        data=ogg,
        filename="nachobot-voice.ogg",
        duration_secs=profile.duration_secs,
        waveform=profile.waveform,
    )


async def prepare_mp3_attachment(
    source: AudioSource,
    *,
    ffmpeg_executable: str,
    max_input_bytes: int = MAX_AUDIO_INPUT_BYTES,
    max_output_bytes: int = MAX_AUDIO_OUTPUT_BYTES,
    require_wav: bool = False,
) -> MP3Attachment:
    """Create a playable MP3 attachment for deferred private interaction replies."""

    if isinstance(source, (bytes, bytearray, memoryview)):
        if len(source) > max_input_bytes:
            raise AudioProcessingError("audio input exceeds its configured bound")
    else:
        try:
            if Path(source).stat().st_size > max_input_bytes:
                raise AudioProcessingError("audio input exceeds its configured bound")
        except OSError as exc:
            raise AudioProcessingError("audio source is unavailable") from exc
    _source_info(source, require_wav=require_wav)
    await _profile_audio(source, ffmpeg_executable)
    mp3 = await _run_ffmpeg(
        ffmpeg_executable,
        source,
        lambda name: (
            *_input_args(name), "-map", "0:a:0", "-vn", "-map_metadata", "-1",
            "-ac", "1", "-ar", "48000", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1",
        ),
        output_limit=min(max_output_bytes, MAX_AUDIO_OUTPUT_BYTES),
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    if not (mp3.startswith(b"ID3") or (len(mp3) > 1 and mp3[0] == 0xFF and mp3[1] & 0xE0 == 0xE0)):
        raise AudioProcessingError("FFmpeg output is not an MP3 stream")
    return MP3Attachment(data=mp3)


def _message_id(value: Any, *, field: str) -> str:
    text = str(value or "")
    if not text.isascii() or not text.isdecimal() or not 17 <= len(text) <= 20:
        raise AudioProcessingError(f"Discord did not return a valid {field}")
    if not 0 < int(text) < 2**64:
        raise AudioProcessingError(f"Discord did not return a valid {field}")
    return text


def _multipart_fields(payload: dict[str, Any], data: bytes, filename: str, content_type: str):
    stream = io.BytesIO(data)
    upload = discord.File(stream, filename=filename)
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    fields = [
        {"name": "payload_json", "value": encoded},
        {
            "name": "files[0]",
            "value": upload.fp,
            "filename": filename,
            "content_type": content_type,
        },
    ]
    return stream, upload, fields


async def send_native_voice_message(
    http_client: Any,
    channel_id: Any,
    voice: VoiceMessageAudio,
    *,
    message_reference: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Send a normal channel voice message using Pycord's authenticated HTTP client."""

    channel = _message_id(channel_id, field="channel id")
    if not voice.data or len(voice.data) > MAX_AUDIO_OUTPUT_BYTES:
        raise AudioProcessingError("encoded voice message is empty or oversized")
    if not math.isfinite(voice.duration_secs) or not MIN_AUDIO_DURATION_SECONDS <= voice.duration_secs <= MAX_AUDIO_DURATION_SECONDS:
        raise AudioProcessingError("voice message duration is invalid")
    payload: dict[str, Any] = {
        "attachments": [
            {
                "id": 0,
                "filename": voice.filename,
                "duration_secs": voice.duration_secs,
                "waveform": voice.waveform,
            }
        ],
        "flags": VOICE_MESSAGE_FLAG,
        "allowed_mentions": {"parse": [], "replied_user": False},
    }
    if message_reference is not None:
        payload["message_reference"] = message_reference
    stream, upload, fields = _multipart_fields(payload, voice.data, voice.filename, "audio/ogg")
    try:
        with _redact_http_secrets(_http_client_token(http_client)):
            response = await http_client.request(
                Route("POST", "/channels/{channel_id}/messages", channel_id=int(channel)),
                files=[upload],
                form=fields,
            )
    finally:
        upload.close()
        stream.close()
    response_channel = _message_id(response.get("channel_id") if isinstance(response, dict) else None, field="channel id")
    if response_channel != channel:
        raise AudioProcessingError("Discord returned a voice message for another channel")
    return {
        "message_id": _message_id(response.get("id"), field="message id"),
        "channel_id": response_channel,
    }


async def send_ephemeral_mp3_followup(
    http_client: Any,
    webhook_id: Any,
    webhook_token: Any,
    channel_id: Any,
    attachment: MP3Attachment,
) -> dict[str, str]:
    """Send a private follow-up through the existing deferred interaction webhook."""

    webhook = _message_id(webhook_id, field="interaction webhook id")
    channel = _message_id(channel_id, field="channel id")
    if not isinstance(webhook_token, str) or not webhook_token or len(webhook_token) > 256:
        raise AudioProcessingError("Discord interaction webhook is unavailable")
    if not attachment.data or len(attachment.data) > MAX_AUDIO_OUTPUT_BYTES:
        raise AudioProcessingError("encoded MP3 attachment is empty or oversized")
    payload = {
        "attachments": [{"id": 0, "filename": attachment.filename}],
        "flags": EPHEMERAL_FLAG,
        "allowed_mentions": {"parse": [], "replied_user": False},
    }
    stream, upload, fields = _multipart_fields(payload, attachment.data, attachment.filename, "audio/mpeg")
    try:
        with _redact_http_secrets(_http_client_token(http_client), webhook_token):
            response = await http_client.request(
                Route(
                    "POST",
                    "/webhooks/{webhook_id}/{webhook_token}",
                    webhook_id=int(webhook),
                    webhook_token=webhook_token,
                ),
                params={"wait": "true"},
                files=[upload],
                form=fields,
            )
    finally:
        upload.close()
        stream.close()
    response_channel = _message_id(response.get("channel_id") if isinstance(response, dict) else None, field="channel id")
    if response_channel != channel:
        raise AudioProcessingError("Discord returned a follow-up for another channel")
    return {
        "message_id": _message_id(response.get("id"), field="message id"),
        "channel_id": response_channel,
    }
