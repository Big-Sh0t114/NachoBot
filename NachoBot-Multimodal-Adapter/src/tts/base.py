"""Shared TTS backend contract and PCM stream helpers."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO
import time
import wave
from typing import AsyncIterable, AsyncIterator


MAX_TTS_AUDIO_SECONDS = 120.0
MAX_TTS_AUDIO_BYTES = 32 * 1024 * 1024
MAX_TTS_STREAM_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class PCMChunk:
    """One typed chunk of interleaved PCM audio."""

    data: bytes
    sample_rate: int
    channels: int = 1
    sample_width: int = 2
    codec: str = "pcm_s16le"


class PCMStreamError(ValueError):
    """An engine returned malformed, empty, or over-budget PCM audio."""


class EmptyPCMStreamError(PCMStreamError):
    """A synthesis completed without returning any audio frames."""


async def close_async_iterator(iterator: object) -> None:
    """Close an async iterator and finish cleanup before propagating cancellation."""

    close = getattr(iterator, "aclose", None)
    if close is None:
        return

    close_task = asyncio.create_task(close())
    cancelled = False
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            if close_task.cancelled():
                raise
            cancelled = True
    close_task.result()
    if cancelled:
        raise asyncio.CancelledError


async def iter_validated_pcm(
    stream: AsyncIterable[PCMChunk],
    *,
    max_audio_seconds: float = MAX_TTS_AUDIO_SECONDS,
    max_audio_bytes: int = MAX_TTS_AUDIO_BYTES,
    max_stream_seconds: float = MAX_TTS_STREAM_SECONDS,
) -> AsyncIterator[PCMChunk]:
    """Validate, frame-align, and bound PCM chunks while preserving streaming."""

    iterator = stream.__aiter__()
    started_at = time.monotonic()
    metadata: tuple[int, int, int, str] | None = None
    pending = bytearray()
    raw_bytes = 0
    aligned_bytes = 0

    try:
        while True:
            remaining = max_stream_seconds - (time.monotonic() - started_at)
            if remaining <= 0:
                raise TimeoutError("TTS stream exceeded its wall-clock limit")
            try:
                chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
            except StopAsyncIteration:
                break

            if not isinstance(chunk, PCMChunk):
                raise PCMStreamError("TTS backend yielded a non-PCMChunk value")
            if not isinstance(chunk.data, bytes):
                raise PCMStreamError("PCM chunk data must be bytes")
            if isinstance(chunk.sample_rate, bool) or not isinstance(chunk.sample_rate, int) or chunk.sample_rate <= 0:
                raise PCMStreamError("PCM sample rate must be a positive integer")
            if isinstance(chunk.channels, bool) or not isinstance(chunk.channels, int) or chunk.channels <= 0:
                raise PCMStreamError("PCM channel count must be a positive integer")
            if chunk.sample_width != 2 or chunk.codec != "pcm_s16le":
                raise PCMStreamError("Only signed little-endian PCM16 audio is supported")

            current_metadata = (chunk.sample_rate, chunk.channels, chunk.sample_width, chunk.codec)
            if metadata is None:
                metadata = current_metadata
            elif metadata != current_metadata:
                raise PCMStreamError("PCM metadata changed during a TTS stream")

            raw_bytes += len(chunk.data)
            if raw_bytes > max_audio_bytes:
                raise PCMStreamError("TTS audio exceeded the byte limit")

            if not chunk.data:
                continue
            frame_width = chunk.channels * chunk.sample_width
            pending.extend(chunk.data)
            aligned_length = len(pending) - (len(pending) % frame_width)
            if aligned_length == 0:
                continue

            aligned_data = bytes(pending[:aligned_length])
            del pending[:aligned_length]
            aligned_bytes += aligned_length
            if aligned_bytes > max_audio_bytes:
                raise PCMStreamError("TTS audio exceeded the byte limit")
            duration = aligned_bytes / frame_width / chunk.sample_rate
            if duration > max_audio_seconds:
                raise PCMStreamError("TTS audio exceeded the duration limit")

            yield PCMChunk(
                data=aligned_data,
                sample_rate=chunk.sample_rate,
                channels=chunk.channels,
                sample_width=chunk.sample_width,
                codec=chunk.codec,
            )

        if pending:
            raise PCMStreamError("TTS stream ended with an incomplete PCM sample frame")
        if aligned_bytes == 0:
            raise EmptyPCMStreamError("TTS returned empty audio")
    finally:
        await close_async_iterator(iterator)


async def collect_pcm_stream_to_wav(stream: AsyncIterable[PCMChunk]) -> bytes:
    """Collect the standard PCM streaming path into a bounded WAV response."""

    validated = iter_validated_pcm(stream)
    buffer = BytesIO()
    pcm_data = bytearray()
    metadata: tuple[int, int, int] | None = None
    try:
        async for chunk in validated:
            if metadata is None:
                metadata = (chunk.channels, chunk.sample_width, chunk.sample_rate)
            pcm_data.extend(chunk.data)
    finally:
        await close_async_iterator(validated)

    if metadata is None or not pcm_data:
        raise EmptyPCMStreamError("TTS returned empty audio")
    with wave.open(buffer, "wb") as wav_file:
        channels, sample_width, sample_rate = metadata
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(pcm_data))
    return buffer.getvalue()


class BaseTTSModel(ABC):
    """Base client interface implemented by each selectable TTS backend."""

    @abstractmethod
    def __init__(self):
        self.config = self.load_config()

    @abstractmethod
    def load_config(self):
        """Load the backend-specific configuration."""

        raise NotImplementedError

    @abstractmethod
    async def tts(self, text: str, **kwargs) -> bytes:
        """Return a complete WAV collected from :meth:`tts_pcm_stream`."""

        raise NotImplementedError

    @abstractmethod
    async def tts_stream(self, text: str, **kwargs) -> AsyncIterator[bytes]:
        """Compatibility byte stream retained for existing backend callers."""

        raise NotImplementedError

    async def tts_pcm_stream(self, text: str, **kwargs) -> AsyncIterator[PCMChunk]:
        """Yield normalized PCM chunks for public streaming and collection."""

        raise NotImplementedError
