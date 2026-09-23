"""Bounded WAV helpers for UniversalVC/Core voice segments."""

from __future__ import annotations

import base64
import io
import os
import tempfile
import wave
from typing import Union

import numpy as np


MAX_WAV_BYTES = 16 * 1024 * 1024


def samples_to_wav_base64(
    samples: np.ndarray,
    *,
    sample_rate: int = 16_000,
    max_duration_seconds: float = 60.0,
) -> str:
    """Encode a bounded mono float32 utterance as signed-16 WAV base64."""

    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    max_samples = max(1, int(sample_rate * max_duration_seconds))
    values = values[:max_samples]
    if values.size == 0 or sample_rate <= 0:
        return ""
    pcm = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    raw = output.getvalue()
    return base64.b64encode(raw).decode("ascii") if len(raw) <= MAX_WAV_BYTES else ""


def decode_wav_base64(value: Union[str, bytes, bytearray]) -> bytes:
    """Decode and validate a Core voice payload without exposing audio data."""

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
    """Persist validated Core audio for the platform playback queue."""

    decoded = decode_wav_base64(value)
    fd, path = tempfile.mkstemp(prefix="nachobot-universal-", suffix=".wav")
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
