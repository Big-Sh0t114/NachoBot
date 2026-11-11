"""Bounded pygame PCM streaming for the sprite renderer.

This module deliberately accepts pygame as a dependency instead of importing
the Live2D renderer (or pygame at module import time). The renderer owns the
pygame mixer lifecycle and calls :meth:`pump` from its render loop.
"""

from __future__ import annotations

import math
import sys
from array import array
from collections import deque
from typing import Any


MAX_PCM_CHUNK_BYTES = 64 * 1024
MAX_PCM_STREAM_BYTES = 32 * 1024 * 1024
MAX_PCM_BUFFERED_BYTES = 512 * 1024
# Keep each render-loop conversion bounded even when the producer delivered a
# full utterance before playback had time to begin.
MAX_PCM_PUMP_INPUT_BYTES = 16 * 1024
MAX_PCM_PUMP_OUTPUT_BYTES = 32 * 1024


class _PCM16Resampler:
    """Incremental PCM16 sample-rate and mono/stereo converter.

    This is the same linear-interpolation converter used by the Live2D
    renderer. Keeping it here lets the sprite backend handle its own PCM
    without importing Live2D code.
    """

    def __init__(
        self,
        source_rate: int,
        source_channels: int,
        output_rate: int,
        output_channels: int,
    ) -> None:
        self.source_rate = source_rate
        self.source_channels = source_channels
        self.output_rate = output_rate
        self.output_channels = output_channels
        self.step = source_rate / float(output_rate)
        self.position = 0.0
        self.pending: list[tuple[int, ...]] = []
        self.input_frames = 0
        self.output_frames = 0

    def convert(self, pcm: bytes) -> bytes:
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        frames = [
            self._map_channels(
                tuple(samples[offset : offset + self.source_channels])
            )
            for offset in range(0, len(samples), self.source_channels)
        ]
        self.input_frames += len(frames)
        combined = self.pending + frames
        output_frames: list[tuple[int, ...]] = []
        while self.position < len(combined) - 1:
            index = int(self.position)
            fraction = self.position - index
            left = combined[index]
            right = combined[index + 1]
            output_frames.append(
                tuple(
                    max(-32768, min(32767, round(a + (b - a) * fraction)))
                    for a, b in zip(left, right, strict=True)
                )
            )
            self.position += self.step
        self.output_frames += len(output_frames)
        consumed = min(int(self.position), max(0, len(combined) - 1))
        self.pending = combined[consumed:]
        self.position -= consumed
        return self._encode(output_frames)

    def estimate_output_bytes(self, input_bytes: int) -> int:
        """Bound the allocation convert() may produce for one chunk."""
        input_frames = input_bytes // (self.source_channels * 2)
        combined_frames = len(self.pending) + input_frames
        positions_before_last_frame = combined_frames - 1 - self.position
        output_frames = (
            max(0, math.ceil(positions_before_last_frame / self.step))
            if positions_before_last_frame > 0
            else 0
        )
        return output_frames * self.output_channels * 2

    def estimate_finish_bytes(self) -> int:
        target_frames = round(self.input_frames * self.output_rate / self.source_rate)
        remaining = max(0, target_frames - self.output_frames)
        return remaining * self.output_channels * 2

    def finish(self) -> bytes:
        target_frames = round(self.input_frames * self.output_rate / self.source_rate)
        remaining = max(0, target_frames - self.output_frames)
        output_frames: list[tuple[int, ...]] = []
        for _ in range(remaining):
            if not self.pending:
                break
            index = min(int(self.position), len(self.pending) - 1)
            fraction = self.position - int(self.position)
            left = self.pending[index]
            right = self.pending[min(index + 1, len(self.pending) - 1)]
            output_frames.append(
                tuple(
                    max(-32768, min(32767, round(a + (b - a) * fraction)))
                    for a, b in zip(left, right, strict=True)
                )
            )
            self.position += self.step
        self._reset()
        return self._encode(output_frames)

    def _map_channels(self, frame: tuple[int, ...]) -> tuple[int, ...]:
        if self.source_channels == self.output_channels:
            return frame
        if self.source_channels == 1:
            return (frame[0], frame[0])
        return ((frame[0] + frame[1]) // 2,)

    def _reset(self) -> None:
        self.position = 0.0
        self.pending = []
        self.input_frames = 0
        self.output_frames = 0

    def _encode(self, frames: list[tuple[int, ...]]) -> bytes:
        samples = array("h", (sample for frame in frames for sample in frame))
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes()


class PygamePCMPlayer:
    """Play bounded ``voice_stream`` PCM events on one pygame channel.

    The mixer must already be initialized. This helper does not initialize or
    shut down pygame; it only stops and releases the dedicated channel it owns.
    Call ``pump`` from the render loop so sounds advance without another thread.
    """

    def __init__(self, pygame: Any, logger: Any) -> None:
        self.pygame = pygame
        self.logger = logger
        self.ready = False
        self._closed = False
        self._output_rate = 0
        self._output_channels = 0
        self._channel = None
        self._stream_id: str | None = None
        self._parent_message_id: str | None = None
        self._sample_rate = 0
        self._channels = 0
        self._sample_width = 0
        self._expected_seq = 0
        self._total_input_bytes = 0
        self._ended = False
        self._resampler: _PCM16Resampler | None = None
        self._raw_chunks: deque[bytes] = deque()
        self._raw_offset = 0
        self._raw_pending_bytes = 0
        self._chunks: deque[bytes] = deque()
        self._pending_bytes = 0
        self._current_sound = None
        self._current_bytes = 0
        self._queued_sound = None
        self._queued_sound_bytes = 0
        self._initialize_channel()

    @property
    def active(self) -> bool:
        """True from an accepted start until its final sound has drained."""
        return self._stream_id is not None

    @property
    def buffered_bytes(self) -> int:
        """Converted PCM bytes held in Python or by the mixer."""
        return self._pending_bytes + self._current_bytes + self._queued_sound_bytes

    @property
    def pending_input_bytes(self) -> int:
        """Raw source PCM waiting for incremental conversion."""
        return self._raw_pending_bytes

    def handle_event(self, event: dict[str, Any]) -> None:
        """Accept one decoded PCM stream event.

        Stale events from a different stream are ignored. A malformed event or
        an ordering/buffer violation aborts the current stream and raises a
        concise ``ValueError`` or ``BufferError`` for the caller to report.
        """
        if self._closed:
            raise RuntimeError("PCM player is closed")
        if not isinstance(event, dict):
            self._abort_with_error("PCM event must be an object")
        operation = event.get("event")
        if operation not in ("start", "chunk", "end", "abort"):
            self._abort_with_error("PCM event must be start, chunk, end, or abort")

        stream_id, parent_id = self._validate_identity(event)
        if operation == "start":
            metadata = self._validate_format(event)
            if not self.ready or self._channel is None:
                self._abort_with_error("PCM output is not ready", exc_type=RuntimeError)
            # Validate the replacement completely before barging in on the
            # currently playing stream.
            self.stop()
            self._stream_id = stream_id
            self._parent_message_id = parent_id
            self._sample_rate = metadata[0]
            self._channels = metadata[1]
            self._sample_width = metadata[2]
            self._expected_seq = 0
            self._total_input_bytes = 0
            self._ended = False
            self._resampler = _PCM16Resampler(
                self._sample_rate,
                self._channels,
                self._output_rate,
                self._output_channels,
            )
            return

        # Events without an active stream, or with another valid identity, are
        # late frames from a stream that was replaced or already drained.
        if self._stream_id is None or (
            stream_id != self._stream_id or parent_id != self._parent_message_id
        ):
            return

        try:
            self._validate_matching_format(event)
            if self._ended:
                self._abort_with_error("PCM event arrived after end")
            if operation == "abort":
                self.stop()
                return
            if operation == "chunk":
                self._handle_chunk(event)
                return
            self._handle_end()
        except (ValueError, BufferError, RuntimeError):
            self.stop()
            raise

    def pump(self) -> None:
        """Move bounded PCM buffers onto the channel and release drained data."""
        if self._closed or not self.ready or self._channel is None:
            return
        try:
            self._pump_resampler()
            if self._channel.get_busy():
                playing = self._channel.get_sound()
                if self._queued_sound is not None and playing is self._queued_sound:
                    # The previous current sound ended and pygame promoted the
                    # queued sound. Its bytes remain buffered until it drains.
                    self._current_sound = self._queued_sound
                    self._current_bytes = self._queued_sound_bytes
                    self._queued_sound = None
                    self._queued_sound_bytes = 0
                if self._queued_sound is None and self._chunks:
                    self._queue_next_sound()
                return

            # An idle channel means all in-flight sounds have drained (or a
            # mixer error dropped them). Release their bounded bookkeeping.
            self._current_sound = None
            self._current_bytes = 0
            self._queued_sound = None
            self._queued_sound_bytes = 0
            if self._chunks:
                self._play_next_sound()
                return
            if self._ended and not self._raw_chunks and self._resampler is None:
                self._clear_stream()
        except Exception as exc:
            self._log(
                "error",
                "Sprite PCM playback failed: {}",
                type(exc).__name__,
            )
            self.stop()

    def stop(self) -> None:
        """Stop immediately and clear all stream data and identity."""
        if self._channel is not None:
            try:
                self._channel.stop()
            except Exception:
                pass
        self._clear_stream()

    def close(self) -> None:
        """Release playback state; the pygame mixer remains caller-owned."""
        if self._closed:
            return
        self.stop()
        self._closed = True
        self.ready = False
        self._channel = None

    def _initialize_channel(self) -> None:
        mixer = getattr(self.pygame, "mixer", None)
        try:
            mixer_format = mixer.get_init() if mixer is not None else None
        except Exception:
            mixer_format = None
        if not isinstance(mixer_format, tuple) or len(mixer_format) != 3:
            return
        sample_rate, sample_format, channels = mixer_format
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or sample_rate < 8000
            or sample_format != -16
            or isinstance(channels, bool)
            or not isinstance(channels, int)
            or channels not in (1, 2)
        ):
            return
        try:
            self._channel = mixer.Channel(0)
        except Exception:
            self._channel = None
            return
        self._output_rate = sample_rate
        self._output_channels = channels
        self.ready = True

    def _handle_chunk(self, event: dict[str, Any]) -> None:
        seq = event.get("seq")
        pcm = event.get("pcm")
        frame_bytes = self._channels * self._sample_width
        if (
            isinstance(seq, bool)
            or not isinstance(seq, int)
            or seq < 0
            or seq != self._expected_seq
        ):
            self._abort_with_error("PCM chunk sequence is invalid or out of order")
        if (
            not isinstance(pcm, bytes)
            or not pcm
            or len(pcm) > MAX_PCM_CHUNK_BYTES
            or len(pcm) % frame_bytes
        ):
            self._abort_with_error("PCM chunk is empty, malformed, or exceeds 64 KiB")
        if self._total_input_bytes + len(pcm) > MAX_PCM_STREAM_BYTES:
            self._abort_with_error("PCM stream exceeded the 32 MiB utterance limit", exc_type=BufferError)
        if self._resampler is None:
            self._abort_with_error("PCM stream resampler is unavailable", exc_type=RuntimeError)

        # Retain only the bounded raw utterance here. Conversion happens in
        # pump(), where it is limited both by the mixer headroom and by a small
        # per-render-loop CPU/output budget.
        self._raw_chunks.append(pcm)
        self._raw_pending_bytes += len(pcm)
        self._total_input_bytes += len(pcm)
        self._expected_seq += 1

    def _handle_end(self) -> None:
        # The source may still be queued for incremental conversion. pump()
        # finishes the resampler only after consuming that raw backlog.
        self._ended = True

    def _pump_resampler(self) -> None:
        """Convert a bounded aligned slice of raw PCM into bounded output."""
        resampler = self._resampler
        if resampler is None:
            return

        room = MAX_PCM_BUFFERED_BYTES - self.buffered_bytes
        output_budget = min(MAX_PCM_PUMP_OUTPUT_BYTES, room)
        pump_output_bytes = 0
        if self._raw_chunks and output_budget > 0:
            source_frame_bytes = self._channels * self._sample_width
            max_input_bytes = min(
                self._raw_pending_bytes,
                MAX_PCM_PUMP_INPUT_BYTES,
            )
            # This pump currently converts only the head chunk. A later queued
            # chunk cannot fill the remainder of a slice taken from this one.
            max_input_bytes = min(
                max_input_bytes,
                len(self._raw_chunks[0]) - self._raw_offset,
            )
            max_frames = max_input_bytes // source_frame_bytes
            # estimate_output_bytes is monotonic in the number of source
            # frames. Binary search avoids rejecting a valid source chunk just
            # because its complete converted form would exceed the mixer cap.
            low = 0
            high = max_frames
            while low < high:
                candidate = (low + high + 1) // 2
                estimated = resampler.estimate_output_bytes(
                    candidate * source_frame_bytes
                )
                if estimated <= output_budget:
                    low = candidate
                else:
                    high = candidate - 1

            if low:
                input_bytes = low * source_frame_bytes
                head = self._raw_chunks[0]
                source = head[self._raw_offset : self._raw_offset + input_bytes]
                try:
                    output = resampler.convert(source)
                except Exception as exc:
                    self.stop()
                    raise RuntimeError("PCM resampling failed") from exc
                if len(output) > room or len(output) > MAX_PCM_PUMP_OUTPUT_BYTES:
                    # The estimate and converter share the same phase math;
                    # this is an internal invariant guard, not an input limit.
                    self.stop()
                    raise RuntimeError("PCM resampler exceeded its bounded pump budget")
                if output:
                    self._chunks.append(output)
                    self._pending_bytes += len(output)
                    pump_output_bytes += len(output)
                self._raw_offset += input_bytes
                self._raw_pending_bytes -= input_bytes
                if self._raw_offset == len(head):
                    self._raw_chunks.popleft()
                    self._raw_offset = 0

        # finish() can only emit the interpolation tail after all source bytes
        # have passed through convert(). The tail is bounded by the resampler's
        # small pending frame set, but still check the public output cap.
        if self._ended and not self._raw_chunks and self._resampler is not None:
            resampler = self._resampler
            available = MAX_PCM_BUFFERED_BYTES - self.buffered_bytes
            pump_output_available = MAX_PCM_PUMP_OUTPUT_BYTES - pump_output_bytes
            tail_size = resampler.estimate_finish_bytes()
            if tail_size <= available and tail_size <= pump_output_available:
                try:
                    tail = resampler.finish()
                except Exception as exc:
                    self.stop()
                    raise RuntimeError("PCM resampling failed") from exc
                self._resampler = None
                if len(tail) > available or len(tail) > pump_output_available:
                    self.stop()
                    raise RuntimeError("PCM resampler tail exceeded its bounded pump budget")
                if tail:
                    self._chunks.append(tail)
                    self._pending_bytes += len(tail)

    def _queue_next_sound(self) -> None:
        pcm = self._chunks.popleft()
        self._pending_bytes -= len(pcm)
        sound = self.pygame.mixer.Sound(buffer=pcm)
        self._channel.queue(sound)
        self._queued_sound = sound
        self._queued_sound_bytes = len(pcm)

    def _play_next_sound(self) -> None:
        pcm = self._chunks.popleft()
        self._pending_bytes -= len(pcm)
        sound = self.pygame.mixer.Sound(buffer=pcm)
        self._channel.play(sound)
        self._current_sound = sound
        self._current_bytes = len(pcm)

    def _validate_identity(self, event: dict[str, Any]) -> tuple[str, str]:
        stream_id = event.get("stream_id")
        parent_id = event.get("parent_message_id")
        if (
            not isinstance(stream_id, str)
            or not stream_id.strip()
            or len(stream_id) > 128
            or not isinstance(parent_id, str)
            or not parent_id.strip()
            or len(parent_id) > 256
        ):
            self._abort_with_error("PCM stream identity is invalid")
        return stream_id.strip(), parent_id.strip()

    def _validate_format(self, event: dict[str, Any]) -> tuple[int, int, int]:
        sample_rate = event.get("sample_rate")
        channels = event.get("channels")
        sample_width = event.get("sample_width")
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or not 8000 <= sample_rate <= 96000
            or isinstance(channels, bool)
            or not isinstance(channels, int)
            or channels not in (1, 2)
            or isinstance(sample_width, bool)
            or not isinstance(sample_width, int)
            or sample_width != 2
            or event.get("codec") != "pcm_s16le"
        ):
            self._abort_with_error("PCM stream format is unsupported")
        return sample_rate, channels, sample_width

    def _validate_matching_format(self, event: dict[str, Any]) -> None:
        metadata = self._validate_format(event)
        if metadata != (self._sample_rate, self._channels, self._sample_width):
            self._abort_with_error("PCM event format does not match the active stream")

    def _abort_with_error(
        self,
        message: str,
        *,
        exc_type: type[Exception] = ValueError,
    ) -> None:
        self.stop()
        raise exc_type(message)

    def _clear_stream(self) -> None:
        self._stream_id = None
        self._parent_message_id = None
        self._sample_rate = 0
        self._channels = 0
        self._sample_width = 0
        self._expected_seq = 0
        self._total_input_bytes = 0
        self._ended = False
        self._resampler = None
        self._raw_chunks.clear()
        self._raw_offset = 0
        self._raw_pending_bytes = 0
        self._chunks.clear()
        self._pending_bytes = 0
        self._current_sound = None
        self._current_bytes = 0
        self._queued_sound = None
        self._queued_sound_bytes = 0

    def _log(self, level: str, message: str, *args: Any) -> None:
        method = getattr(self.logger, level, None)
        if method is not None:
            method(message, *args)
