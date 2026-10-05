"""Bounded WAV helpers for the Discord/Core voice contract.

The adapter owns capture, but it does not own speech recognition.  Discord
packets are therefore collected into one short WAV utterance and sent to Core
as a base64 ``voice`` segment.  Keeping the codec here also gives the outbound
path one place to validate Core-produced audio before handing it to ffmpeg.
"""

from __future__ import annotations

import base64
import io
import os
import tempfile
import wave
from typing import Union


MAX_WAV_BYTES = 16 * 1024 * 1024


def pcm16_to_wav_base64(
    pcm_data: bytes,
    *,
    sample_rate: int = 48_000,
    channels: int = 2,
    max_duration_seconds: float = 60.0,
) -> str:
    """Encode bounded signed-16 PCM into a valid base64 WAV payload."""

    if not pcm_data or sample_rate <= 0 or channels <= 0:
        return ""

    frame_width = channels * 2
    max_frames = max(1, int(sample_rate * max_duration_seconds))
    max_pcm_bytes = max_frames * frame_width
    pcm_data = bytes(pcm_data[:max_pcm_bytes])
    pcm_data = pcm_data[: len(pcm_data) - (len(pcm_data) % frame_width)]
    if not pcm_data:
        return ""

    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return encoded if len(output.getvalue()) <= MAX_WAV_BYTES else ""


def decode_wav_base64(value: Union[str, bytes, bytearray]) -> bytes:
    """Decode and validate a Core voice payload without logging its contents."""

    if isinstance(value, str):
        raw_value = value.encode("ascii")
    elif isinstance(value, (bytes, bytearray)):
        raw_value = bytes(value)
    else:
        raise ValueError("voice payload must be base64 text")

    decoded = base64.b64decode(raw_value, validate=True)
    if not decoded or len(decoded) > MAX_WAV_BYTES:
        raise ValueError("voice payload is empty or exceeds the size limit")
    try:
        with wave.open(io.BytesIO(decoded), "rb") as wav_file:
            if wav_file.getnchannels() <= 0 or wav_file.getframerate() <= 0:
                raise ValueError("voice payload has invalid WAV metadata")
            wav_file.readframes(1)
    except (wave.Error, EOFError) as exc:
        raise ValueError("voice payload is not a valid WAV") from exc
    return decoded


def write_wav_base64(value: Union[str, bytes, bytearray]) -> str:
    """Persist validated Core audio for the platform playback API."""

    decoded = decode_wav_base64(value)
    fd, path = tempfile.mkstemp(prefix="nachobot-discord-", suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(decoded)
    except Exception:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path
