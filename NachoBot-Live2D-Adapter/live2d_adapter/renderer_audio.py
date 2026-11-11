"""Bounded WAV and PCM playback owned by the standalone renderer thread."""

from __future__ import annotations

import io
import math
import queue
import sys
import threading
from array import array
from collections import deque
from collections.abc import Callable
from typing import Any

MAX_PCM_PENDING_BYTES = 512 * 1024
MAX_PCM_STREAM_TOTAL_BYTES = 32 * 1024 * 1024
MAX_PCM_CHUNK_BYTES = 64 * 1024
MAX_QUEUED_WAV_BLOCKS = 64
MAX_QUEUED_WAV_BYTES = 32 * 1024 * 1024
MAX_VOICE_EVENTS_PER_FRAME = 128


class _PCM16Resampler:
    """Incremental PCM16 sample-rate and mono/stereo converter."""

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
            self._map_channels(tuple(samples[offset : offset + self.source_channels]))
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
        input_frames = input_bytes // (self.source_channels * 2)
        combined_frames = len(self.pending) + input_frames
        positions_before_last_frame = combined_frames - 1 - self.position
        output_frames = (
            max(0, math.ceil(positions_before_last_frame / self.step))
            if positions_before_last_frame > 0
            else 0
        )
        return output_frames * self.output_channels * 2

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


class RendererAudio:
    """Audio queues and mixer lifecycle; call methods on the SDL render thread."""

    def __init__(
        self,
        pygame_module: Any,
        logger: Any,
        voice_stream_queue: queue.Queue[dict[str, Any]] | None = None,
    ) -> None:
        self.pygame = pygame_module
        self.logger = logger
        self.voice_stream_queue = voice_stream_queue or queue.Queue(maxsize=64)
        self.audio_ready_event = threading.Event()
        self.pcm_audio_ready = False
        self.is_speaking = False
        self._audio_channel: Any = None
        self._current_sound: Any = None
        self._queued_sounds: deque[tuple[Any, int]] = deque()
        self._queued_wav_bytes = 0
        self._audio_auto_speaking = False
        self._pcm_audio_channel: Any = None
        self._pcm_current_sound: Any = None
        self._pcm_queued_sound: Any = None
        self._pcm_chunks: deque[bytes] = deque()
        self._pcm_queued_bytes = 0
        self._pcm_stream_id: str | None = None
        self._pcm_parent_message_id: str | None = None
        self._pcm_sample_rate = 0
        self._pcm_channels = 0
        self._pcm_expected_seq = 0
        self._pcm_total_bytes = 0
        self._pcm_sample_width = 0
        self._pcm_end_pending = False
        self._pcm_resampler: _PCM16Resampler | None = None
        self._pcm_output_rate = 0
        self._pcm_output_channels = 0

    def initialize(self) -> bool:
        """Try mixer setup and signal that the audio capability probe completed."""

        try:
            if not self.pygame.mixer.get_init():
                self.pygame.mixer.init()
            self.refresh_pcm_audio_readiness()
        except Exception as exc:
            self._log("warning", f"[Live2D] Audio device unavailable: {exc}")
            self.pcm_audio_ready = False
            self._pcm_output_rate = 0
            self._pcm_output_channels = 0
        finally:
            self.audio_ready_event.set()
        return self.pcm_audio_ready

    @property
    def audio_ready(self) -> bool:
        """Whether ordinary WAV playback currently has a real mixer device."""

        try:
            return bool(self.pygame.mixer.get_init())
        except Exception:
            return False

    def command(self, kind: str, payload: Any) -> bool:
        """Handle one renderer-owned audio command; return false for other kinds."""

        if kind == "voice_stream":
            self.handle_voice_stream_event(payload)
            return True
        if kind in {"play_audio", "play_chat_audio"}:
            if self.play_audio(payload):
                self.is_speaking = True
                self._audio_auto_speaking = True
            return True
        if kind == "queue_audio":
            self.queue_audio(payload)
            return True
        if kind == "stop_audio":
            self.stop_audio()
            return True
        if kind == "speaking":
            self.is_speaking = bool(payload)
            return True
        return False

    def pump(self) -> None:
        """Consume bounded voice events and advance both playback queues."""

        self.pump_voice_events()
        self.pump_playback()

    def pump_voice_events(self) -> None:
        """Drain the bounded event queue before renderer commands, as before."""

        for _ in range(MAX_VOICE_EVENTS_PER_FRAME):
            try:
                event = self.voice_stream_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle_voice_stream_event(event)
            except Exception as exc:
                self._log("warning", f"[Live2D] Rejected PCM audio event: {exc}")
                self._abort_pcm_stream()

    def pump_playback(self) -> None:
        """Advance queued PCM and WAV after the command queue is processed."""

        self._pump_pcm_stream()
        self._advance_audio_queue()

    def pump_pcm_stream(self) -> None:
        """Advance only the PCM channel before renderer commands are handled."""

        self._pump_pcm_stream()

    def advance_wav_queue(self) -> None:
        """Advance queued WAV blocks after renderer commands are handled."""

        self._advance_audio_queue()

    def play_audio(self, audio_data: Any) -> bool:
        """Play one WAV payload and interrupt any existing WAV or PCM output."""

        if not isinstance(audio_data, bytes) or not audio_data:
            self._log("warning", "[Live2D] Ignored empty or invalid audio command")
            return False
        try:
            self._ensure_mixer()
            self.stop_audio()
            sound = self.pygame.mixer.Sound(file=io.BytesIO(audio_data))
            channel = sound.play()
            if channel is None:
                self._log("warning", "[Live2D] No PyGame mixer channel is available for TTS")
                return False
            self._current_sound = sound
            self._audio_channel = channel
            return True
        except Exception as exc:
            self._log("error", f"[Live2D] Failed to play TTS audio: {exc}")
            return False

    def queue_audio(self, item: Any) -> bool:
        """Queue one bounded WAV block without interrupting its predecessor."""

        if not isinstance(item, dict):
            self._log("warning", "[Live2D] Ignored invalid queued audio command")
            return False
        audio_data = item.get("audio")
        if not isinstance(audio_data, bytes) or not audio_data:
            self._log("warning", "[Live2D] Ignored empty queued audio block")
            return False
        if len(audio_data) > MAX_QUEUED_WAV_BYTES:
            self._log("warning", "[Live2D] Ignored oversized queued WAV block")
            return False

        try:
            self._ensure_mixer()
            if item.get("reset"):
                self.stop_audio()
            queued_bytes = sum(size for _, size in self._queued_sounds)
            if (
                len(self._queued_sounds) >= MAX_QUEUED_WAV_BLOCKS
                or queued_bytes + len(audio_data) > MAX_QUEUED_WAV_BYTES
            ):
                self._log("warning", "[Live2D] Queued WAV capacity exceeded")
                return False
            sound = self.pygame.mixer.Sound(file=io.BytesIO(audio_data))
            if self._audio_channel is not None and self._audio_channel.get_busy():
                self._queued_sounds.append((sound, len(audio_data)))
                self._queued_wav_bytes += len(audio_data)
            else:
                channel = sound.play()
                if channel is None:
                    self._log(
                        "warning",
                        "[Live2D] No PyGame mixer channel is available for streamed TTS",
                    )
                    return False
                self._current_sound = sound
                self._audio_channel = channel
            self.is_speaking = True
            self._audio_auto_speaking = True
            return True
        except Exception as exc:
            self._log("error", f"[Live2D] Failed to queue streamed TTS audio: {exc}")
            return False

    def _advance_audio_queue(self) -> None:
        if not self._audio_auto_speaking:
            return
        if self._audio_channel is not None and self._audio_channel.get_busy():
            return
        if self._queued_sounds:
            sound, size = self._queued_sounds.popleft()
            self._queued_wav_bytes = max(0, self._queued_wav_bytes - size)
            channel = sound.play()
            if channel is not None:
                self._current_sound = sound
                self._audio_channel = channel
                return
            self._log("warning", "[Live2D] Could not continue streamed TTS playback")
        self.stop_audio()

    def stop_audio(self) -> None:
        self._abort_pcm_stream()
        channel = self._audio_channel
        if channel is not None:
            try:
                channel.stop()
            except Exception:
                pass
        self._audio_channel = None
        self._current_sound = None
        self._queued_sounds.clear()
        self._queued_wav_bytes = 0
        if self._audio_auto_speaking:
            self.is_speaking = False
        self._audio_auto_speaking = False

    def shutdown(self) -> None:
        self.stop_audio()
        self.audio_ready_event.set()
        # pygame.quit() follows immediately in the owning renderer. Do not leave
        # a stale PCM capability visible after its mixer device is closed.
        self.pcm_audio_ready = False
        self._pcm_output_rate = 0
        self._pcm_output_channels = 0

    def handle_voice_stream_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            raise ValueError("Live2D PCM event must be an object")
        operation = event.get("event")
        if operation == "start":
            stream_id = event.get("stream_id")
            parent_id = event.get("parent_message_id")
            sample_rate = event.get("sample_rate")
            channels = event.get("channels")
            sample_width = event.get("sample_width")
            if (
                not isinstance(stream_id, str)
                or not stream_id.strip()
                or not isinstance(parent_id, str)
                or not parent_id.strip()
                or isinstance(sample_rate, bool)
                or not isinstance(sample_rate, int)
                or not 8000 <= sample_rate <= 96000
                or isinstance(channels, bool)
                or not isinstance(channels, int)
                or channels not in (1, 2)
                or sample_width != 2
                or event.get("codec") != "pcm_s16le"
            ):
                raise ValueError("Live2D PCM start metadata is invalid")
            if not self.pcm_audio_ready:
                raise RuntimeError("Live2D PCM output was not ready")
            self.stop_audio()
            try:
                self._pcm_audio_channel = self.pygame.mixer.Channel(0)
            except Exception as exc:
                self.pcm_audio_ready = False
                self._abort_pcm_stream()
                raise RuntimeError("Live2D PCM playback channel is unavailable") from exc
            self._pcm_stream_id = stream_id.strip()
            self._pcm_parent_message_id = parent_id.strip()
            self._pcm_sample_rate = sample_rate
            self._pcm_channels = channels
            self._pcm_sample_width = sample_width
            self._pcm_resampler = _PCM16Resampler(
                sample_rate,
                channels,
                self._pcm_output_rate,
                self._pcm_output_channels,
            )
            self._pcm_expected_seq = 0
            self._pcm_total_bytes = 0
            self._pcm_end_pending = False
            self.is_speaking = True
            return

        if not self._matches_pcm_stream(event):
            self._log("warning", "[Live2D] Ignored PCM event for an unknown stream")
            return
        if operation == "chunk":
            seq = event.get("seq")
            pcm = event.get("pcm")
            if (
                isinstance(seq, bool)
                or not isinstance(seq, int)
                or seq != self._pcm_expected_seq
                or not isinstance(pcm, bytes)
                or not pcm
                or len(pcm) > MAX_PCM_CHUNK_BYTES
                or len(pcm) % (self._pcm_channels * self._pcm_sample_width)
            ):
                raise ValueError("Live2D PCM chunk is invalid or out of order")
            if self._pcm_total_bytes + len(pcm) > MAX_PCM_STREAM_TOTAL_BYTES:
                raise BufferError("Live2D PCM stream exceeded its total limit")
            assert self._pcm_resampler is not None
            if (
                self._pcm_queued_bytes + self._pcm_resampler.estimate_output_bytes(len(pcm))
                > MAX_PCM_PENDING_BYTES
            ):
                raise BufferError("Live2D PCM playback queue is full")
            output_pcm = self._pcm_resampler.convert(pcm)
            if self._pcm_queued_bytes + len(output_pcm) > MAX_PCM_PENDING_BYTES:
                raise BufferError("Live2D PCM playback queue is full")
            if output_pcm:
                self._pcm_chunks.append(output_pcm)
                self._pcm_queued_bytes += len(output_pcm)
            self._pcm_total_bytes += len(pcm)
            self._pcm_expected_seq += 1
            return

        if operation == "end":
            if self._pcm_resampler is not None:
                tail = self._pcm_resampler.finish()
                self._pcm_resampler = None
                if self._pcm_queued_bytes + len(tail) > MAX_PCM_PENDING_BYTES:
                    raise BufferError("Live2D PCM playback queue is full")
                if tail:
                    self._pcm_chunks.append(tail)
                    self._pcm_queued_bytes += len(tail)
            self._pcm_end_pending = True
            return
        if operation == "abort":
            self._abort_pcm_stream()
            return
        raise ValueError("Live2D PCM event is unsupported")

    def _matches_pcm_stream(self, event: dict[str, Any]) -> bool:
        return bool(
            self._pcm_stream_id is not None
            and event.get("stream_id") == self._pcm_stream_id
            and event.get("parent_message_id") == self._pcm_parent_message_id
            and event.get("sample_rate") == self._pcm_sample_rate
            and event.get("channels") == self._pcm_channels
            and event.get("sample_width") == self._pcm_sample_width
            and event.get("codec") == "pcm_s16le"
        )

    def _pump_pcm_stream(self) -> None:
        channel = self._pcm_audio_channel
        if channel is None or self._pcm_stream_id is None:
            return
        try:
            if channel.get_busy():
                current = channel.get_sound()
                if current is self._pcm_queued_sound:
                    self._pcm_current_sound = self._pcm_queued_sound
                    self._pcm_queued_sound = None
                if self._pcm_queued_sound is None and self._pcm_chunks:
                    pcm = self._pcm_chunks.popleft()
                    self._pcm_queued_bytes -= len(pcm)
                    queued_sound = self.pygame.mixer.Sound(buffer=pcm)
                    channel.queue(queued_sound)
                    self._pcm_queued_sound = queued_sound
                return

            self._pcm_current_sound = None
            self._pcm_queued_sound = None
            if self._pcm_chunks:
                pcm = self._pcm_chunks.popleft()
                self._pcm_queued_bytes -= len(pcm)
                sound = self.pygame.mixer.Sound(buffer=pcm)
                channel.play(sound)
                self._pcm_current_sound = sound
                return
            if self._pcm_end_pending:
                self._clear_pcm_stream()
        except Exception as exc:
            self._log("error", f"[Live2D] PCM playback failed: {type(exc).__name__}")
            self._abort_pcm_stream()

    def refresh_pcm_audio_readiness(self) -> bool:
        mixer_format = self.pygame.mixer.get_init()
        if not mixer_format:
            self.pcm_audio_ready = False
            self._pcm_output_rate = 0
            self._pcm_output_channels = 0
            return False
        sample_rate, sample_format, channels = mixer_format
        self._pcm_output_rate = int(sample_rate)
        self._pcm_output_channels = int(channels)
        self.pcm_audio_ready = bool(
            self._pcm_output_rate >= 8000
            and sample_format == -16
            and self._pcm_output_channels in (1, 2)
        )
        return self.pcm_audio_ready

    def _ensure_mixer(self) -> None:
        if not self.pygame.mixer.get_init():
            self.pygame.mixer.init()
        self.refresh_pcm_audio_readiness()

    def _abort_pcm_stream(self) -> None:
        channel = self._pcm_audio_channel
        if channel is not None:
            try:
                channel.stop()
            except Exception:
                pass
        self._clear_pcm_stream()

    def _clear_pcm_stream(self) -> None:
        self._pcm_audio_channel = None
        self._pcm_current_sound = None
        self._pcm_queued_sound = None
        self._pcm_chunks.clear()
        self._pcm_queued_bytes = 0
        self._pcm_stream_id = None
        self._pcm_parent_message_id = None
        self._pcm_sample_rate = 0
        self._pcm_channels = 0
        self._pcm_sample_width = 0
        self._pcm_expected_seq = 0
        self._pcm_total_bytes = 0
        self._pcm_end_pending = False
        self._pcm_resampler = None
        self.is_speaking = False

    def _log(self, level: str, message: str) -> None:
        method: Callable[..., Any] | None = getattr(self.logger, level, None)
        if callable(method):
            method(message)
