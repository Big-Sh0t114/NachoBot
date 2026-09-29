import asyncio
import base64
import io
import os
import queue
import tempfile
import threading
import time
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Deque, Mapping, Optional

import numpy as np

try:
    import winsound
except ImportError:  # Linux containers use the remote Live2D playback path.
    winsound = None

try:
    import sounddevice as _sounddevice
except (ImportError, OSError):  # Keep buffered and remote playback importable.
    _sounddevice = None


MAX_VOICE_STREAM_CHUNK_BYTES = 64 * 1024
MAX_VOICE_STREAM_BUFFER_BYTES = 512 * 1024
MAX_VOICE_STREAM_TOTAL_BYTES = 32 * 1024 * 1024


class _PCMLinearResampler:
    """Stateful PCM16 resampling and mono/stereo mapping between chunks."""

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
        self._step = source_rate / float(output_rate)
        self._position = 0.0
        self._pending = np.empty((0, output_channels), dtype=np.float64)
        self._total_input_frames = 0
        self._total_output_frames = 0

    def convert(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        frames = np.frombuffer(pcm, dtype="<i2").reshape(-1, self.source_channels)
        values = frames.astype(np.float64)
        if self.source_channels == 1 and self.output_channels == 2:
            values = np.repeat(values, 2, axis=1)
        elif self.source_channels == 2 and self.output_channels == 1:
            values = values.mean(axis=1, keepdims=True)
        combined = np.concatenate((self._pending, values), axis=0)
        positions = np.arange(self._position, len(combined) - 1, self._step)
        if len(positions):
            indexes = np.floor(positions).astype(np.int64)
            fractions = (positions - indexes)[:, None]
            output = combined[indexes] + (
                combined[indexes + 1] - combined[indexes]
            ) * fractions
            self._position += len(positions) * self._step
            self._total_output_frames += len(positions)
        else:
            output = np.empty((0, self.output_channels), dtype=np.float64)
        self._total_input_frames += len(frames)
        consumed = min(int(self._position), max(0, len(combined) - 1))
        self._pending = combined[consumed:]
        self._position -= consumed
        return self._encode(output)

    def finish(self) -> bytes:
        target_frames = int(
            round(self._total_input_frames * self.output_rate / self.source_rate)
        )
        remaining_frames = max(0, target_frames - self._total_output_frames)
        if not len(self._pending) or not remaining_frames:
            self._reset()
            return b""
        positions = self._position + np.arange(remaining_frames) * self._step
        indexes = np.minimum(
            np.floor(positions).astype(np.int64), len(self._pending) - 1
        )
        right_indexes = np.minimum(indexes + 1, len(self._pending) - 1)
        fractions = (positions - indexes)[:, None]
        output = self._pending[indexes] + (
            self._pending[right_indexes] - self._pending[indexes]
        ) * fractions
        result = self._encode(output)
        self._reset()
        return result

    def _reset(self) -> None:
        self._pending = np.empty((0, self.output_channels), dtype=np.float64)
        self._position = 0.0
        self._total_input_frames = 0
        self._total_output_frames = 0

    @staticmethod
    def _encode(samples: np.ndarray) -> bytes:
        if not len(samples):
            return b""
        return np.clip(np.rint(samples), -32768, 32767).astype("<i2").tobytes()


class _SoundDevicePCMOutput:
    """Persistent callback-driven PCM output with a bounded ring buffer."""

    def __init__(self, sample_rate: int, channels: int) -> None:
        if _sounddevice is None:
            raise RuntimeError("sounddevice PCM playback is unavailable")
        device = _sounddevice.query_devices(kind="output")
        self.sample_rate = int(round(float(device["default_samplerate"])))
        max_channels = int(device.get("max_output_channels", 0))
        if max_channels < 1:
            raise RuntimeError("default audio device has no output channels")
        self.channels = min(2, max_channels)
        _sounddevice.check_output_settings(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
        )
        self._resampler = _PCMLinearResampler(
            sample_rate,
            channels,
            self.sample_rate,
            self.channels,
        )
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._finished = False
        self._awaiting_playback_drain = False
        self._drained = threading.Event()
        self._stream = _sounddevice.RawOutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=0,
            callback=self._on_audio,
        )
        try:
            self._stream.start()
        except Exception:
            self._stream.close()
            raise

    def _on_audio(self, outdata, frames, _time_info, _status) -> None:
        requested = frames * self.channels * 2
        with self._lock:
            available = min(requested, len(self._buffer))
            if available:
                outdata[:available] = self._buffer[:available]
                del self._buffer[:available]
                self._awaiting_playback_drain = True
            if available < requested:
                outdata[available:requested] = b"\0" * (requested - available)
            if available == 0:
                self._awaiting_playback_drain = False
            if self._finished and not self._buffer and available == 0:
                self._drained.set()

    def write(self, pcm: bytes) -> bool:
        output_pcm = self._resampler.convert(pcm)
        with self._lock:
            if self._finished or len(self._buffer) + len(output_pcm) > MAX_VOICE_STREAM_BUFFER_BYTES:
                return False
            self._buffer.extend(output_pcm)
            return True

    def finish(self) -> None:
        output_pcm = self._resampler.finish()
        with self._lock:
            if len(self._buffer) + len(output_pcm) > MAX_VOICE_STREAM_BUFFER_BYTES:
                raise BufferError("resampled PCM output buffer is full")
            self._buffer.extend(output_pcm)
            self._finished = True
            if not self._buffer and not self._awaiting_playback_drain:
                self._drained.set()

    def wait_until_drained(self, timeout: float) -> bool:
        return self._drained.wait(timeout)

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            if stream.active:
                stream.stop()
        finally:
            stream.close()

    def abort(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._finished = True
            self._drained.set()
        self.close()


@dataclass(slots=True)
class _VoiceStreamSession:
    stream_id: str
    parent_message_id: str
    sample_rate: int
    channels: int
    sample_width: int
    codec: str
    remote: bool
    output: Any = None
    expected_seq: int = 0
    total_bytes: int = 0
    ended: bool = False
    first_chunk_at: float | None = None
    playback_queued_until: float | None = None


class AudioPlayer:
    """
    Manages audio playback with support for queuing, interruption, and resuming.
    When a Live2D playback callback is available, it asks the renderer process
    to play the audio so OBS can associate the sound with that window. Falls
    back to winsound when the remote renderer is unavailable.
    """

    def __init__(
        self,
        logger,
        on_start=None,
        on_stop=None,
        remote_playback_callback: Optional[Callable[[bytes], Awaitable[bool]]] = None,
        remote_stop_callback: Optional[Callable[[], Awaitable[bool]]] = None,
        remote_voice_stream_callback: Optional[
            Callable[[Mapping[str, Any]], Awaitable[bool]]
        ] = None,
        remote_voice_stream_ready_callback: Optional[Callable[[], bool]] = None,
        pcm_output_factory: Optional[Callable[[int, int], Any]] = None,
    ):
        self.logger = logger
        self.on_start = on_start
        self.on_stop = on_stop
        self.remote_playback_callback = remote_playback_callback
        self.remote_stop_callback = remote_stop_callback
        self.remote_voice_stream_callback = remote_voice_stream_callback
        self.remote_voice_stream_ready_callback = remote_voice_stream_ready_callback
        self.pcm_output_factory = pcm_output_factory
        self.queue: Deque[tuple[bytes, bool]] = queue.deque() # (audio_data, is_idle)
        self.current_audio: Optional[bytes] = None
        self.interrupted_audio: Optional[bytes] = None
        self.is_playing = False
        self._is_idle_audio = False
        self.is_paused = False
        self.stop_event = asyncio.Event()  # Set when stopped/interrupted
        self.play_task: Optional[asyncio.Task] = None
        self._loop = None
        self._voice_stream: _VoiceStreamSession | None = None
        self._playback_generation = 0
        self._voice_stream_lock = asyncio.Lock()
        self._buffered_idle_event = asyncio.Event()
        self._buffered_idle_event.set()

    @property
    def can_stream_voice(self) -> bool:
        """Whether a Live TTS request currently has a known PCM output path."""
        if self.remote_voice_stream_callback is not None:
            try:
                if (
                    self.remote_voice_stream_ready_callback is not None
                    and self.remote_voice_stream_ready_callback()
                ):
                    return True
            except Exception:
                pass
        if self.pcm_output_factory is not None:
            return True
        if _sounddevice is None:
            return False
        try:
            device = _sounddevice.query_devices(kind="output")
            if not isinstance(device, Mapping):
                return False
            output_rate = int(round(float(device["default_samplerate"])))
            output_channels = min(2, int(device.get("max_output_channels", 0)))
            if output_channels < 1:
                return False
            _sounddevice.check_output_settings(
                samplerate=output_rate,
                channels=output_channels,
                dtype="int16",
            )
            return bool(device and int(device.get("max_output_channels", 0)) > 0)
        except Exception:
            return False

    @property
    def active_voice_stream_id(self) -> str | None:
        session = self._voice_stream
        return session.stream_id if session is not None else None

    @staticmethod
    def _validated_stream_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
        event = payload.get("event")
        if not isinstance(event, str) or event not in ("start", "chunk", "end", "abort"):
            raise ValueError("voice_stream event is invalid")
        stream_id = payload.get("stream_id")
        parent_message_id = payload.get("parent_message_id")
        if (
            not isinstance(stream_id, str)
            or not stream_id.strip()
            or len(stream_id) > 128
        ):
            raise ValueError("voice_stream stream_id is invalid")
        if (
            not isinstance(parent_message_id, str)
            or not parent_message_id.strip()
            or len(parent_message_id) > 256
        ):
            raise ValueError("voice_stream parent_message_id is invalid")

        sample_rate = payload.get("sample_rate")
        channels = payload.get("channels")
        sample_width = payload.get("sample_width")
        codec = payload.get("codec")
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or not 8000 <= sample_rate <= 96000
        ):
            raise ValueError("voice_stream sample_rate is invalid")
        if (
            isinstance(channels, bool)
            or not isinstance(channels, int)
            or channels not in (1, 2)
        ):
            raise ValueError("voice_stream channels must be one or two")
        if (
            isinstance(sample_width, bool)
            or not isinstance(sample_width, int)
            or sample_width != 2
        ):
            raise ValueError("voice_stream sample_width must be 2")
        if codec != "pcm_s16le":
            raise ValueError("voice_stream codec must be pcm_s16le")

        return {
            "event": event,
            "stream_id": stream_id.strip(),
            "parent_message_id": parent_message_id.strip(),
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
            "codec": codec,
        }

    @staticmethod
    def _decode_stream_chunk(payload: Mapping[str, Any], channels: int) -> bytes:
        encoded = payload.get("audio_base64")
        max_chars = ((MAX_VOICE_STREAM_CHUNK_BYTES + 2) // 3) * 4
        if not isinstance(encoded, str) or not encoded or len(encoded) > max_chars:
            raise ValueError("voice_stream chunk is empty or exceeds the size limit")
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("voice_stream chunk is not valid base64") from exc
        if (
            not pcm
            or len(pcm) > MAX_VOICE_STREAM_CHUNK_BYTES
            or len(pcm) % (channels * 2)
        ):
            raise ValueError("voice_stream chunk does not contain bounded PCM frames")
        return pcm

    def _new_pcm_output(self, sample_rate: int, channels: int) -> Any:
        if self.pcm_output_factory is not None:
            return self.pcm_output_factory(sample_rate, channels)
        return _SoundDevicePCMOutput(sample_rate, channels)

    async def handle_voice_stream_event(self, payload: Any) -> bool:
        """Validate and play one ordered Core PCM lifecycle event."""
        if not isinstance(payload, Mapping):
            self.logger.warning("Ignored malformed Core voice_stream event")
            return False
        try:
            metadata = self._validated_stream_metadata(payload)
            event = metadata["event"]
            if event == "chunk":
                seq = payload.get("seq")
                if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                    raise ValueError("voice_stream seq is invalid")
                pcm = self._decode_stream_chunk(payload, metadata["channels"])
            else:
                seq = None
                pcm = None
        except ValueError as exc:
            self.logger.warning("Ignored invalid Core voice_stream event: {}", exc)
            return False

        async with self._voice_stream_lock:
            if event == "start":
                return await self._start_voice_stream(metadata)
            session = self._voice_stream
            if not self._matches_stream(session, metadata):
                self.logger.warning("Ignored Core voice_stream event for an unknown stream")
                return False
            if event == "chunk":
                return await self._write_voice_stream_chunk(session, metadata, seq, pcm)
            if event == "end":
                return await self._end_voice_stream(session)
            return await self._abort_voice_stream_locked(session, send_remote=True)

    @staticmethod
    def _matches_stream(
        session: _VoiceStreamSession | None,
        metadata: Mapping[str, Any],
    ) -> bool:
        return bool(
            session is not None
            and session.stream_id == metadata["stream_id"]
            and session.parent_message_id == metadata["parent_message_id"]
            and session.sample_rate == metadata["sample_rate"]
            and session.channels == metadata["channels"]
            and session.sample_width == metadata["sample_width"]
            and session.codec == metadata["codec"]
        )

    def _remote_stream_ready(self) -> bool:
        if (
            self.remote_voice_stream_callback is None
            or self.remote_voice_stream_ready_callback is None
        ):
            return False
        try:
            return bool(self.remote_voice_stream_ready_callback())
        except Exception:
            return False

    @staticmethod
    def _remote_event(metadata: Mapping[str, Any], event: str, **fields: Any) -> dict[str, Any]:
        payload = dict(metadata)
        payload["event"] = event
        payload.update(fields)
        return payload

    async def _start_voice_stream(self, metadata: dict[str, Any]) -> bool:
        previous = self._voice_stream
        if previous is not None:
            await self._abort_voice_stream_locked(
                previous,
                send_remote=True,
                notify_stop=False,
            )

        # A new Core utterance takes the output immediately and discards stale
        # buffered/idle media so it cannot speak over the stream.
        self.queue.clear()
        self.interrupted_audio = None
        self._playback_generation += 1
        if self.is_playing:
            self.stop_event.set()
            await self._stop_sound()
            if not self._buffered_idle_event.is_set():
                try:
                    await asyncio.wait_for(self._buffered_idle_event.wait(), 1.0)
                except asyncio.TimeoutError:
                    self.logger.warning("Buffered audio did not stop before PCM stream start")

        if self._remote_stream_ready():
            assert self.remote_voice_stream_callback is not None
            try:
                accepted = await self.remote_voice_stream_callback(
                    self._remote_event(metadata, "start")
                )
            except Exception as exc:
                self.logger.warning(
                    "Live2D PCM stream start failed: {}", type(exc).__name__
                )
                accepted = False
            if accepted:
                self._voice_stream = _VoiceStreamSession(
                    **self._metadata_for_payload(metadata),
                    remote=True,
                )
                self.is_playing = True
                return True
            try:
                await self.remote_voice_stream_callback(
                    self._remote_event(metadata, "abort")
                )
            except Exception:
                pass

        try:
            output = self._new_pcm_output(metadata["sample_rate"], metadata["channels"])
        except Exception as exc:
            self.logger.warning(
                "PCM playback unavailable for Core voice_stream: {}",
                type(exc).__name__,
            )
            self.is_playing = False
            return False
        self._voice_stream = _VoiceStreamSession(
            **self._metadata_for_payload(metadata),
            remote=False,
            output=output,
        )
        self.is_playing = True
        self._notify_callback(self.on_start)
        return True

    async def _write_voice_stream_chunk(
        self,
        session: _VoiceStreamSession,
        metadata: Mapping[str, Any],
        seq: int,
        pcm: bytes,
    ) -> bool:
        if session.ended:
            self.logger.warning("Ignored Core voice_stream chunk after end")
            return False
        if seq != session.expected_seq:
            self.logger.warning(
                "Aborting out-of-order Core voice_stream: expected_seq={} received_seq={}",
                session.expected_seq,
                seq,
            )
            await self._abort_voice_stream_locked(session, send_remote=True)
            return False
        if session.total_bytes + len(pcm) > MAX_VOICE_STREAM_TOTAL_BYTES:
            self.logger.warning("Aborting oversized Core voice_stream")
            await self._abort_voice_stream_locked(session, send_remote=True)
            return False

        if session.remote:
            callback = self.remote_voice_stream_callback
            if callback is None:
                await self._abort_voice_stream_locked(session, send_remote=False)
                return False
            try:
                accepted = await callback(
                    self._remote_event(metadata, "chunk", seq=seq, pcm=pcm)
                )
            except Exception:
                accepted = False
            if not accepted:
                self.logger.warning("Live2D PCM stream transport stopped")
                await self._abort_voice_stream_locked(session, send_remote=True)
                return False
        else:
            try:
                accepted = session.output.write(pcm)
            except Exception:
                accepted = False
            if accepted is False:
                self.logger.warning("Local PCM output buffer is full; aborting stream")
                await self._abort_voice_stream_locked(session, send_remote=False)
                return False

        session.expected_seq += 1
        chunk_received_at = time.monotonic()
        if session.first_chunk_at is None:
            session.first_chunk_at = chunk_received_at
        if session.remote:
            queued_from = max(
                chunk_received_at,
                session.playback_queued_until or chunk_received_at,
            )
            session.playback_queued_until = queued_from + len(pcm) / float(
                session.sample_rate * session.channels * session.sample_width
            )
        session.total_bytes += len(pcm)
        return True

    async def _end_voice_stream(self, session: _VoiceStreamSession) -> bool:
        if session.ended:
            return False
        session.ended = True
        if session.remote:
            callback = self.remote_voice_stream_callback
            if callback is None:
                await self._abort_voice_stream_locked(session, send_remote=False)
                return False
            metadata = self._metadata_for_session(session)
            try:
                accepted = await callback(self._remote_event(metadata, "end"))
            except Exception:
                accepted = False
            if not accepted:
                await self._abort_voice_stream_locked(session, send_remote=True)
                return False
            remaining = max(
                0.0,
                (session.playback_queued_until or time.monotonic())
                - time.monotonic(),
            ) + 0.12
            loop = self._loop
            if loop is None or loop.is_closed():
                await self._abort_voice_stream_locked(session, send_remote=True)
                return False
            loop.create_task(self._finish_remote_voice_stream(session, remaining))
            return True

        try:
            session.output.finish()
        except Exception:
            await self._abort_voice_stream_locked(session, send_remote=False)
            return False
        loop = self._loop
        if loop is None or loop.is_closed():
            await self._abort_voice_stream_locked(session, send_remote=False)
            return False
        loop.create_task(self._finish_local_voice_stream(session))
        return True

    async def _finish_remote_voice_stream(
        self,
        session: _VoiceStreamSession,
        remaining_seconds: float,
    ) -> None:
        await asyncio.sleep(remaining_seconds)
        async with self._voice_stream_lock:
            if self._voice_stream is not session:
                return
            self._voice_stream = None
            self.is_playing = False
        self._notify_callback(self.on_stop)

    async def _finish_local_voice_stream(self, session: _VoiceStreamSession) -> None:
        duration = session.total_bytes / float(
            session.sample_rate * session.channels * session.sample_width
        )
        timeout = min(900.0, max(2.0, duration + 2.0))
        drained = False
        try:
            drained = await asyncio.to_thread(
                session.output.wait_until_drained,
                timeout,
            )
        except Exception:
            pass
        if not drained:
            self.logger.warning("Local PCM stream did not drain before its timeout")
        try:
            session.output.close()
        except Exception:
            pass
        async with self._voice_stream_lock:
            if self._voice_stream is not session:
                return
            self._voice_stream = None
            self.is_playing = False
        self._notify_callback(self.on_stop)

    async def _abort_voice_stream_locked(
        self,
        session: _VoiceStreamSession,
        *,
        send_remote: bool,
        notify_stop: bool = True,
    ) -> bool:
        if self._voice_stream is session:
            self._voice_stream = None
        self.is_playing = False
        if session.remote:
            callback = self.remote_voice_stream_callback
            if send_remote and callback is not None:
                try:
                    await callback(
                        self._remote_event(self._metadata_for_session(session), "abort")
                    )
                except Exception:
                    pass
        else:
            try:
                session.output.abort()
            except Exception:
                try:
                    session.output.close()
                except Exception:
                    pass
        if notify_stop:
            self._notify_callback(self.on_stop)
        return True

    async def abort_voice_stream(self) -> bool:
        async with self._voice_stream_lock:
            session = self._voice_stream
            if session is None:
                return False
            return await self._abort_voice_stream_locked(session, send_remote=True)

    async def handle_remote_disconnect(self) -> None:
        async with self._voice_stream_lock:
            session = self._voice_stream
            if session is None or not session.remote:
                return
            await self._abort_voice_stream_locked(session, send_remote=False)

    async def shutdown(self) -> None:
        await self.abort_voice_stream()
        self.is_paused = True
        self.queue.clear()
        self.stop_event.set()
        task = self.play_task
        self.play_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._stop_sound()

    @staticmethod
    def _metadata_for_payload(metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: metadata[key]
            for key in (
                "stream_id",
                "parent_message_id",
                "sample_rate",
                "channels",
                "sample_width",
                "codec",
            )
        }

    @staticmethod
    def _metadata_for_session(session: _VoiceStreamSession) -> dict[str, Any]:
        return {
            "stream_id": session.stream_id,
            "parent_message_id": session.parent_message_id,
            "sample_rate": session.sample_rate,
            "channels": session.channels,
            "sample_width": session.sample_width,
            "codec": session.codec,
        }

    def _notify_callback(self, callback) -> None:
        if callback is None:
            return
        try:
            result = callback()
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception as exc:
            self.logger.warning("Audio callback failed: {}", type(exc).__name__)

    def start(self):
        """Start the playback loop."""
        if self.play_task and not self.play_task.done():
            return
        self._loop = asyncio.get_running_loop()
        self.stop_event.clear()
        self.play_task = self._loop.create_task(self._playback_loop())
        self.logger.info("AudioPlayer started")

    async def _playback_loop(self):
        while True:
            try:
                if self.is_paused:
                    await asyncio.sleep(0.1)
                    continue

                if self._voice_stream is not None:
                    await asyncio.sleep(0.02)
                    continue

                if not self.queue:
                    await asyncio.sleep(0.1)
                    continue

                # Get next audio
                audio_data, is_idle = self.queue.popleft()
                self.current_audio = audio_data
                self.is_playing = True
                self._is_idle_audio = is_idle
                self._playback_generation += 1
                playback_generation = self._playback_generation
                self._buffered_idle_event.clear()

                # Calculate duration
                duration = self._get_wav_duration(audio_data)
                # self.logger.debug(f"Playing audio segment ({duration:.2f}s), idle: {is_idle}")

                # Play (Async)
                if self.on_start:
                    if asyncio.iscoroutinefunction(self.on_start):
                        asyncio.create_task(self.on_start())
                    else:
                        self.on_start()
                await self._play_sound(audio_data)

                # Wait for duration (or interruption)
                # We wait for duration, checking stop_event periodically or using wait_for
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=duration)
                    # If we got here, stop_event was set (Interrupted!)
                    self.logger.info("Audio playback interrupted!")
                    await self._stop_sound()
                    self.interrupted_audio = self.current_audio  # Save current
                except asyncio.TimeoutError:
                    # Finished playing naturally
                    pass
                finally:
                    # Guarantee state reset even if exception occurs
                    if playback_generation == self._playback_generation:
                        self.is_playing = self._voice_stream is not None
                        if self._voice_stream is None:
                            self._notify_callback(self.on_stop)
                    self.current_audio = None
                    self._is_idle_audio = False
                    self._buffered_idle_event.set()
                    self.stop_event.clear()  # Reset for next

            except Exception as e:
                self.logger.error(f"AudioPlayer loop error: {e}")
                await asyncio.sleep(1)

    async def _play_sound(self, audio_data: bytes) -> None:
        """Play through Live2D when connected, otherwise use the local fallback."""
        if self.remote_playback_callback is not None:
            try:
                if await self.remote_playback_callback(audio_data):
                    return
            except Exception as e:
                self.logger.warning(f"Live2D audio playback failed; using winsound: {e}")

        self._play_sound_locally(audio_data)

    def _play_sound_locally(self, audio_data: bytes) -> None:
        if winsound is None:
            self.logger.warning(
                "Local winsound playback is unavailable on this platform; "
                "configure the remote Live2D audio callback for container use."
            )
            return
        try:
            # Save to temp
            temp_path = os.path.join(tempfile.gettempdir(), "nachobot_tts_player.wav")
            with open(temp_path, "wb") as f:
                f.write(audio_data)
            winsound.PlaySound(temp_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            self.logger.error(f"Winsound play error: {e}")

    async def _stop_sound(self) -> None:
        if self.remote_stop_callback is not None:
            try:
                if await self.remote_stop_callback():
                    return
            except Exception as e:
                self.logger.warning(f"Live2D audio stop failed; stopping winsound: {e}")

        if winsound is None:
            return

        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def _get_wav_duration(self, audio_data: bytes) -> float:
        try:
            with io.BytesIO(audio_data) as f:
                with wave.open(f, "rb") as wav_file:
                    frames = wav_file.getnframes()
                    rate = wav_file.getframerate()
                    return frames / float(rate)
        except Exception:
            return 2.0  # Fallback

    def play(self, audio_data: bytes):
        """Add normal audio to queue."""
        self.queue.append((audio_data, False))

    def play_idle(self, audio_data: bytes):
        """Add idle audio to queue."""
        self.queue.append((audio_data, True))

    def interrupt_idle(self):
        """Immediately stop playback if the current audio is idle, and remove all queued idle audio."""
        # 1. Remove all pending idle audio from the queue
        self.queue = queue.deque([(data, is_idle) for (data, is_idle) in self.queue if not is_idle])
        
        # 2. If currently playing an idle audio, interrupt it
        if self.is_playing and self._is_idle_audio:
            self.logger.info("Interrupting currently playing idle audio for a normal reply.")
            self.stop_event.set()

    def stop_and_pause(self):
        """Stop current playback immediately and pause."""
        self.is_paused = True
        self.queue.clear()
        self.stop_event.set()  # Signal loop to stop waiting
        self._schedule_stream_abort()
        self.logger.info("AudioPlayer stopped and paused.")

    def _schedule_stream_abort(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            loop.create_task(self.abort_voice_stream())
        else:
            loop.call_soon_threadsafe(
                lambda: loop.create_task(self.abort_voice_stream())
            )

    def resume(self):
        """Resume playback, re-queueing interrupted audio.
           Note: Interrupted audio may be idle audio, but usually resume() isn't mixing with idle interruption 
           logic directly. If needed, interrupted_audio could also store the is_idle flag.
           For now we assume it's normal audio or we don't care.
        """
        if self.interrupted_audio:
            self.logger.info("Resuming interrupted audio...")
            self.queue.appendleft((self.interrupted_audio, False))
            self.interrupted_audio = None
        self.is_paused = False
        self.stop_event.clear()  # Ensure clear
        self.logger.info("AudioPlayer resumed.")
