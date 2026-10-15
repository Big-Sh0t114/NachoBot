"""Bounded live PCM source for Discord's 20 ms audio pull loop."""

from __future__ import annotations

import threading

import discord
import numpy as np

DISCORD_FRAME_BYTES = 48_000 * 2 * 2 // 50
MAX_SOURCE_BUFFER_BYTES = 48_000 * 2 * 2 * 3


class PCMStreamSource(discord.AudioSource):
    """Convert a Core PCM stream into Discord's 48 kHz stereo PCM frames."""

    def __init__(self, stream_id: str, sample_rate: int, channels: int, sample_width: int = 2):
        if not stream_id or not 8_000 <= sample_rate <= 192_000 or channels not in (1, 2) or sample_width != 2:
            raise ValueError("invalid TTS PCM format")
        self.stream_id = stream_id
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.next_seq = 0
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._input_frames = 0
        self._next_output_position = 0.0
        self._last_frame: np.ndarray | None = None
        self._ended = False
        self._aborted = False

    def is_opus(self) -> bool:
        return False

    def _convert(self, pcm: bytes) -> bytes:
        frames = np.frombuffer(pcm, dtype="<i2").reshape(-1, self.channels).astype(np.float32)
        if self._last_frame is None:
            samples = frames
            start = self._input_frames
        else:
            samples = np.concatenate((self._last_frame[None, :], frames), axis=0)
            start = self._input_frames - 1
        self._input_frames += len(frames)
        self._last_frame = frames[-1].copy()
        return self._resample(samples, start)

    def _resample(self, samples: np.ndarray, start: int) -> bytes:
        last_position = start + len(samples) - 1
        step = self.sample_rate / 48_000
        count = max(0, int(np.ceil((last_position - self._next_output_position) / step)))
        if count == 0:
            return b""
        positions = self._next_output_position + step * np.arange(count, dtype=np.float64)
        # Floating point rounding at a chunk boundary may reach the last
        # sample; keep that sample for the next chunk's interpolation pair.
        positions = positions[positions < last_position]
        if len(positions) == 0:
            return b""
        indices = np.floor(positions - start).astype(np.int64)
        fraction = ((positions - start) - indices).astype(np.float32)[:, None]
        output = samples[indices] * (1.0 - fraction) + samples[indices + 1] * fraction
        self._next_output_position = float(positions[-1] + step)
        if self.channels == 1:
            output = np.repeat(output, 2, axis=1)
        return np.clip(np.rint(output), -32768, 32767).astype("<i2").tobytes()

    def feed(self, seq: int, pcm: bytes) -> None:
        if isinstance(seq, bool) or not isinstance(seq, int) or seq != self.next_seq:
            raise ValueError("TTS stream sequence conflict")
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % (self.channels * 2):
            raise ValueError("invalid TTS PCM chunk")
        if len(pcm) > 64 * 1024:
            raise ValueError("TTS PCM chunk too large")
        with self._lock:
            if self._ended or self._aborted:
                raise ValueError("TTS stream is closed")
            output = self._convert(pcm)
            if len(self._buffer) + len(output) > MAX_SOURCE_BUFFER_BYTES:
                self._aborted = True
                self._buffer.clear()
                raise ValueError("TTS stream playback buffer is full")
            self._buffer.extend(output)
            self.next_seq += 1

    def finish(self) -> None:
        with self._lock:
            if self._aborted or self._ended:
                return
            if self._last_frame is not None:
                self._buffer.extend(self._resample(np.repeat(self._last_frame[None, :], 2, axis=0), self._input_frames - 1))
            self._ended = True

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            self._ended = True
            self._buffer.clear()

    def cleanup(self) -> None:
        self.abort()

    def read(self) -> bytes:
        with self._lock:
            if self._aborted:
                return b""
            if len(self._buffer) >= DISCORD_FRAME_BYTES:
                frame = bytes(self._buffer[:DISCORD_FRAME_BYTES])
                del self._buffer[:DISCORD_FRAME_BYTES]
                return frame
            if self._ended:
                if not self._buffer:
                    return b""
                frame = bytes(self._buffer)
                self._buffer.clear()
                return frame.ljust(DISCORD_FRAME_BYTES, b"\x00")
            return b"\x00" * DISCORD_FRAME_BYTES
