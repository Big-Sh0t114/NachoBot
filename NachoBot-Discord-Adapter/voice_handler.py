"""Discord voice capture, VAD, and bounded Core voice-segment creation."""

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from typing import Callable, Optional

import numpy as np
from config import AdapterConfig, VoiceConfig
from discord.sinks import Filters, Sink
from voice_codec import pcm16_to_wav_base64


# Discord sends stereo 48 kHz, signed 16-bit PCM.
DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
DISCORD_WIDTH = 2
MAX_UTTERANCE_SECONDS = 60.0
MAX_TRACKED_USERS = 32
MAX_ACTIVE_UTTERANCES = 16
MAX_STREAM_AUDIO_EVENTS = 1024
MAX_STREAM_CONTROL_EVENTS = MAX_ACTIVE_UTTERANCES * 2 + 8
STREAM_INPUT_CHUNK_SECONDS = 0.16


class SilenceDetectingSink(Sink):
    """Turn Discord PCM packets into ordered Core voice lifecycle events."""

    PREROLL_SECONDS = 0.3

    def __init__(
        self,
        filters=None,
        callback: Optional[Callable] = None,
        on_speech_start_callback: Optional[Callable] = None,
        on_stream_start_callback: Optional[Callable] = None,
        on_stream_audio_callback: Optional[Callable] = None,
        on_stream_finish_callback: Optional[Callable] = None,
        on_stream_abort_callback: Optional[Callable] = None,
        on_capture_start_callback: Optional[Callable] = None,
        on_capture_audio_callback: Optional[Callable] = None,
        on_capture_finish_callback: Optional[Callable] = None,
        on_capture_abort_callback: Optional[Callable] = None,
        capture_filter: Optional[Callable[[int], bool]] = None,
        config: Optional[VoiceConfig] = None,
    ):
        super().__init__(filters=filters)

        # Core callbacks run on the Discord event loop. Decoder-thread capture
        # callbacks only append bounded PCM to VoiceHandler's bytearrays; WAV
        # encoding and Core transport stay off py-cord's DecodeManager thread.
        self.callback = callback
        self.on_speech_start_callback = on_speech_start_callback
        self.on_stream_start_callback = on_stream_start_callback
        self.on_stream_audio_callback = on_stream_audio_callback
        self.on_stream_finish_callback = on_stream_finish_callback
        self.on_stream_abort_callback = on_stream_abort_callback
        # Full-rate stereo PCM is retained by VoiceHandler synchronously from
        # the decoder thread. These callbacks only take a bounded bytearray lock;
        # all Core networking stays on the async stream worker.
        self.on_capture_start_callback = on_capture_start_callback
        self.on_capture_audio_callback = on_capture_audio_callback
        self.on_capture_finish_callback = on_capture_finish_callback
        self.on_capture_abort_callback = on_capture_abort_callback
        self.capture_filter = capture_filter
        self.config = config
        self.vc = None
        self.audio_data = {}

        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()

        self.last_speech_time: dict[int, float] = {}
        self.is_speaking: dict[int, bool] = {}
        self.utterance_bytes: dict[int, int] = {}
        self.voiced_bytes: dict[int, int] = {}
        self.pre_roll_buffer: dict[int, deque[bytes]] = {}
        self.pre_roll_bytes: dict[int, int] = {}
        self.last_user_activity: dict[int, float] = {}
        self._capture_ids: dict[int, str] = {}
        self._active_capture_ids: set[str] = set()
        self._stream_audio_buffers: dict[str, bytearray] = {}

        self.vad_threshold = config.vad_threshold if config else 500
        self.silence_threshold = config.silence_threshold if config else 0.5
        self.min_speech_duration = 0.3
        self._bytes_per_second = (
            DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * DISCORD_WIDTH
        )
        self._max_preroll_bytes = int(
            self._bytes_per_second * self.PREROLL_SECONDS
        )
        self._stream_audio_chunk_bytes = int(
            self._bytes_per_second * STREAM_INPUT_CHUNK_SECONDS
        )

        self._state_lock = threading.RLock()
        # The queue only carries downsampled-path lifecycle/audio work. WAV
        # capture is independent, so a saturated Core stream queue degrades to
        # the original complete WAV instead of dropping captured audio.
        self._event_queue: asyncio.Queue = asyncio.Queue(
            maxsize=MAX_STREAM_AUDIO_EVENTS + MAX_STREAM_CONTROL_EVENTS
        )
        self._event_reservation_lock = threading.Lock()
        self._queued_audio_events = 0
        self._queued_control_events = 0
        self._failed_streams: set[str] = set()
        self._started_streams: set[str] = set()
        self.checker_task: Optional[asyncio.Task] = None
        self.stream_worker_task: Optional[asyncio.Task] = None
        self._processing_event = None
        self._stopped = False
        self._shutdown_queued = False

        logging.getLogger("VoiceHandler").info(
            "SilenceDetectingSink initialized with Core voice segments"
        )

    def init(self, vc):
        self.vc = vc
        self.loop.call_soon_threadsafe(self._ensure_tasks)

    def _ensure_tasks(self) -> None:
        if self.checker_task is None:
            self.checker_task = self.loop.create_task(self._silence_checker())
        if self.stream_worker_task is None:
            self.stream_worker_task = self.loop.create_task(
                self._stream_event_worker()
            )

    def cleanup(self):
        """Request non-blocking cleanup for Sink compatibility."""
        self._stopped = True
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._begin_shutdown)

    async def aclose(self) -> None:
        """Drain queued audio and abort any unfinished streams."""
        self._stopped = True
        self._begin_shutdown()

        if self.checker_task:
            with suppress(asyncio.CancelledError):
                await self.checker_task
        if self.stream_worker_task:
            with suppress(asyncio.CancelledError):
                await self.stream_worker_task
        while True:
            try:
                event = self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._release_event_reservation(event)
                self._event_queue.task_done()
        with self._state_lock:
            remaining_captures = list(self._active_capture_ids)
        for capture_id in remaining_captures:
            await self._abort_core_stream(capture_id)
            await self._abort_fallback_capture(capture_id)
            self._release_capture_id(capture_id)

    def _begin_shutdown(self) -> None:
        if self._shutdown_queued:
            return
        self._shutdown_queued = True
        self._ensure_tasks()

        if self.checker_task:
            self.checker_task.cancel()

        with self._state_lock:
            active_captures = set(self._active_capture_ids)
            active_captures.update(
                capture_id
                for capture_id in self._capture_ids.values()
                if capture_id
            )
            for user in self.is_speaking:
                self.is_speaking[user] = False
            self._capture_ids.clear()
            self._stream_audio_buffers.clear()

        # Preserve ordered work for captures that already reached a terminal
        # event. Incomplete captures discard their partial Core stream and WAV.
        queued = []
        while True:
            try:
                event = self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._event_queue.task_done()
                queued.append(event)
        terminal_ids = {
            event[2]
            for event in queued
            if event[0] in {"finish", "discard", "abort"} and event[2]
        }
        processing = self._processing_event
        if processing and processing[0] in {"finish", "discard", "abort"} and processing[2]:
            terminal_ids.add(processing[2])
        for event in queued:
            if event[0] == "stop" or event[2] in terminal_ids:
                self._event_queue.put_nowait(event)
            else:
                self._release_event_reservation(event)
        with self._event_reservation_lock:
            unfinished_ids = active_captures - terminal_ids
            self._failed_streams.update(unfinished_ids)
        for capture_id in unfinished_ids:
            self._queue_from_audio_thread(("abort", 0, capture_id, None))
        self._queue_from_audio_thread(("stop", None, None, None))

    def _append_preroll(self, user: int, pcm_data: bytes) -> None:
        if len(pcm_data) > self._max_preroll_bytes:
            pcm_data = pcm_data[-self._max_preroll_bytes :]
        buffer = self.pre_roll_buffer.setdefault(user, deque())
        buffer.append(pcm_data)
        total = self.pre_roll_bytes.get(user, 0) + len(pcm_data)
        while total > self._max_preroll_bytes and len(buffer) > 1:
            total -= len(buffer.popleft())
        self.pre_roll_bytes[user] = total

    def _take_preroll(self, user: int) -> list[bytes]:
        chunks = list(self.pre_roll_buffer.get(user, ()))
        self.pre_roll_buffer[user] = deque()
        self.pre_roll_bytes[user] = 0
        return chunks

    def _forget_user(self, user: int) -> None:
        self.last_speech_time.pop(user, None)
        self.is_speaking.pop(user, None)
        self.utterance_bytes.pop(user, None)
        self.voiced_bytes.pop(user, None)
        self.pre_roll_buffer.pop(user, None)
        self.pre_roll_bytes.pop(user, None)
        self.last_user_activity.pop(user, None)
        self._capture_ids.pop(user, None)

    def _ensure_user_state(self, user: int, now: float) -> bool:
        if user not in self.is_speaking:
            if len(self.is_speaking) >= MAX_TRACKED_USERS:
                inactive = [
                    candidate
                    for candidate, speaking in self.is_speaking.items()
                    if not speaking
                ]
                if not inactive:
                    return False
                oldest = min(
                    inactive,
                    key=lambda candidate: self.last_user_activity.get(
                        candidate, 0.0
                    ),
                )
                self._forget_user(oldest)
            self.is_speaking[user] = False
        self.last_user_activity[user] = now
        return True

    def _capture_start(self, capture_id: str, user_id: int) -> bool:
        if not self.on_capture_start_callback:
            return False
        try:
            started = bool(self.on_capture_start_callback(capture_id, user_id))
        except Exception:
            logging.getLogger("VoiceHandler").exception(
                "Failed to start fallback WAV capture"
            )
            return False
        if started:
            self._active_capture_ids.add(capture_id)
        return started

    def _capture_audio(self, capture_id: str, pcm_data: bytes) -> None:
        if not self.on_capture_audio_callback or not pcm_data:
            return
        try:
            self.on_capture_audio_callback(capture_id, pcm_data)
        except Exception:
            logging.getLogger("VoiceHandler").exception(
                "Failed to retain fallback WAV audio"
            )

    def _add_stream_audio_locked(
        self, user: int, capture_id: str, pcm_data: bytes
    ) -> None:
        if not pcm_data or self._stream_failed(capture_id):
            return
        buffer = self._stream_audio_buffers.setdefault(capture_id, bytearray())
        buffer.extend(pcm_data)
        while len(buffer) >= self._stream_audio_chunk_bytes:
            chunk = bytes(buffer[: self._stream_audio_chunk_bytes])
            del buffer[: self._stream_audio_chunk_bytes]
            self._queue_from_audio_thread(("audio", user, capture_id, chunk))

    def _flush_stream_audio_locked(self, user: int, capture_id: str) -> None:
        buffer = self._stream_audio_buffers.pop(capture_id, None)
        if buffer and not self._stream_failed(capture_id):
            self._queue_from_audio_thread(
                ("audio", user, capture_id, bytes(buffer))
            )

    def _reserve_event(self, event) -> bool:
        event_name, _, capture_id, _ = event
        audio_event = event_name == "audio"
        with self._event_reservation_lock:
            if self._shutdown_queued and event_name not in {"abort", "stop"}:
                return False
            if audio_event:
                if self._queued_audio_events >= MAX_STREAM_AUDIO_EVENTS:
                    if capture_id:
                        self._failed_streams.add(capture_id)
                    return False
                self._queued_audio_events += 1
            else:
                if self._queued_control_events >= MAX_STREAM_CONTROL_EVENTS:
                    if capture_id:
                        self._failed_streams.add(capture_id)
                    return False
                self._queued_control_events += 1
        return True

    def _release_event_reservation(self, event) -> None:
        event_name = event[0]
        with self._event_reservation_lock:
            if event_name == "audio":
                self._queued_audio_events = max(0, self._queued_audio_events - 1)
            else:
                self._queued_control_events = max(
                    0, self._queued_control_events - 1
                )

    def _enqueue_reserved_event(self, event) -> None:
        if self._shutdown_queued and event[0] not in {"abort", "stop"}:
            self._release_event_reservation(event)
            return
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            self._release_event_reservation(event)
            capture_id = event[2]
            if capture_id:
                with self._event_reservation_lock:
                    self._failed_streams.add(capture_id)
            logging.getLogger("VoiceHandler").warning(
                "Discord Core stream queue is full; retaining the original WAV"
            )

    def _queue_from_audio_thread(
        self,
        event: tuple[str, Optional[int], Optional[str], Optional[bytes]],
    ) -> bool:
        if not self.loop or not self.loop.is_running():
            return False
        if not self._reserve_event(event):
            return False
        try:
            self.loop.call_soon_threadsafe(self._enqueue_reserved_event, event)
            return True
        except RuntimeError:
            self._release_event_reservation(event)
            return False

    def _stream_failed(self, capture_id: str) -> bool:
        with self._event_reservation_lock:
            return capture_id in self._failed_streams

    def _clear_stream_failure(self, capture_id: str) -> None:
        with self._event_reservation_lock:
            self._failed_streams.discard(capture_id)

    @Filters.container
    def write(self, data, user):
        """Receive one Discord PCM packet from py-cord's decoder thread."""
        if self._stopped:
            return

        pcm_data = data.pcm if hasattr(data, "pcm") else data
        if not pcm_data:
            return
        if hasattr(user, "id"):
            user = user.id
        try:
            user = int(user)
        except (TypeError, ValueError):
            return
        if self.capture_filter is not None:
            try:
                if not self.capture_filter(user):
                    return
            except Exception:
                logging.getLogger("VoiceHandler").warning(
                    "Discord voice capture policy failed closed"
                )
                return

        try:
            samples = np.frombuffer(pcm_data, dtype=np.int16)
            rms = (
                int(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
                if samples.size
                else 0
            )
        except Exception:
            rms = 0

        is_speech = rms > self.vad_threshold
        now = time.time()
        notify_speech_start = False

        with self._state_lock:
            if not self._ensure_user_state(user, now):
                return
            speaking = self.is_speaking[user]

            if not speaking and not is_speech:
                self._append_preroll(user, pcm_data)

            if is_speech:
                self.last_speech_time[user] = now
                if not speaking:
                    active_count = sum(self.is_speaking.values())
                    if active_count >= MAX_ACTIVE_UTTERANCES:
                        # Keep only bounded pre-roll for this user. A later
                        # utterance can be accepted after another user finishes.
                        return

                    self.is_speaking[user] = True
                    notify_speech_start = True
                    preroll = self._take_preroll(user)
                    self.utterance_bytes[user] = sum(map(len, preroll)) + len(
                        pcm_data
                    )
                    self.voiced_bytes[user] = len(pcm_data)
                    capture_id = uuid.uuid4().hex
                    if self._capture_start(capture_id, user):
                        self._capture_ids[user] = capture_id
                        self._stream_audio_buffers[capture_id] = bytearray()
                        self._queue_from_audio_thread(
                            ("start", user, capture_id, None)
                        )
                        for chunk in preroll:
                            self._capture_audio(capture_id, chunk)
                            self._add_stream_audio_locked(user, capture_id, chunk)
                        self._capture_audio(capture_id, pcm_data)
                        self._add_stream_audio_locked(user, capture_id, pcm_data)
                    logging.getLogger("VoiceHandler").debug(
                        "User %s started speaking (RMS: %s)", user, rms
                    )
                else:
                    self.utterance_bytes[user] = (
                        self.utterance_bytes.get(user, 0) + len(pcm_data)
                    )
                    self.voiced_bytes[user] = (
                        self.voiced_bytes.get(user, 0) + len(pcm_data)
                    )
                    capture_id = self._capture_ids.get(user)
                    if capture_id:
                        self._capture_audio(capture_id, pcm_data)
                        self._add_stream_audio_locked(user, capture_id, pcm_data)
            elif speaking:
                # Keep feeding trailing silence until VAD closes the stream.
                self.utterance_bytes[user] = (
                    self.utterance_bytes.get(user, 0) + len(pcm_data)
                )
                capture_id = self._capture_ids.get(user)
                if capture_id:
                    self._capture_audio(capture_id, pcm_data)
                    self._add_stream_audio_locked(user, capture_id, pcm_data)

        if (
            notify_speech_start
            and self.on_speech_start_callback
            and self.loop
            and self.loop.is_running()
        ):
            asyncio.run_coroutine_threadsafe(
                self.on_speech_start_callback(user),
                self.loop,
            )

    async def _silence_checker(self) -> None:
        logging.getLogger("VoiceHandler").info("Silence checker started")
        while not self._stopped:
            try:
                await asyncio.sleep(0.1)
                now = time.time()
                completed: list[tuple[str, int, Optional[str]]] = []

                with self._state_lock:
                    for user, speaking in list(self.is_speaking.items()):
                        if not speaking:
                            continue
                        silence_duration = now - self.last_speech_time.get(user, 0)
                        total_bytes = self.utterance_bytes.get(user, 0)
                        reached_limit = (
                            total_bytes >= self._bytes_per_second
                            * MAX_UTTERANCE_SECONDS
                        )
                        if (
                            silence_duration <= self.silence_threshold
                            and not reached_limit
                        ):
                            continue

                        self.is_speaking[user] = False
                        total_duration = total_bytes / self._bytes_per_second
                        self.utterance_bytes.pop(user, None)
                        voiced_duration = (
                            self.voiced_bytes.pop(user, 0)
                            / self._bytes_per_second
                        )
                        self.last_speech_time.pop(user, None)
                        capture_id = self._capture_ids.pop(user, None)

                        event_name = (
                            "finish"
                            if voiced_duration >= self.min_speech_duration
                            else "discard"
                        )
                        if capture_id:
                            if event_name == "finish":
                                self._flush_stream_audio_locked(user, capture_id)
                            else:
                                self._stream_audio_buffers.pop(capture_id, None)
                        completed.append((event_name, user, capture_id))
                        logging.getLogger("VoiceHandler").info(
                            "Discord speech ended for %s: %.2fs total, "
                            "%.2fs voiced%s",
                            user,
                            total_duration,
                            voiced_duration,
                            " (duration limit)" if reached_limit else "",
                        )

                for event in completed:
                    self._queue_from_audio_thread(
                        (event[0], event[1], event[2], None)
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger("VoiceHandler").exception(
                    "Silence checker error"
                )
                await asyncio.sleep(0.2)

    async def _stream_event_worker(self) -> None:
        """Run bounded, ordered Core stream work without blocking capture."""
        while True:
            event = await self._event_queue.get()
            event_name, user, capture_id, pcm_data = event
            self._processing_event = event
            self._release_event_reservation(event)
            try:
                if event_name == "stop":
                    break

                if event_name == "start":
                    started = False
                    if capture_id and not self._stream_failed(capture_id):
                        started = bool(
                            self.on_stream_start_callback
                            and await self.on_stream_start_callback(capture_id, user)
                        )
                    if started:
                        self._started_streams.add(capture_id)
                        if self._stream_failed(capture_id):
                            await self._abort_core_stream(capture_id)
                    elif capture_id:
                        self._mark_stream_failed(capture_id)
                elif event_name == "audio":
                    if capture_id in self._started_streams:
                        if self._stream_failed(capture_id):
                            await self._abort_core_stream(capture_id)
                        elif self.on_stream_audio_callback and pcm_data:
                            try:
                                sent = await self.on_stream_audio_callback(
                                    capture_id, pcm_data
                                )
                            except Exception:
                                self._mark_stream_failed(capture_id)
                                await self._abort_core_stream(capture_id)
                                raise
                            if sent is False:
                                self._mark_stream_failed(capture_id)
                            if self._stream_failed(capture_id):
                                await self._abort_core_stream(capture_id)
                elif event_name == "finish":
                    voice_data = None
                    result_id = None
                    if capture_id:
                        if capture_id in self._started_streams:
                            if self._stream_failed(capture_id):
                                await self._abort_core_stream(capture_id)
                            elif self.on_stream_finish_callback:
                                try:
                                    result_id = await self.on_stream_finish_callback(
                                        capture_id
                                    )
                                except Exception:
                                    logging.getLogger("VoiceHandler").exception(
                                        "Failed to finalize Core stream for user %s",
                                        user,
                                    )
                            self._started_streams.discard(capture_id)
                        if self.on_capture_finish_callback:
                            try:
                                voice_data = await self.on_capture_finish_callback(
                                    capture_id
                                )
                            except Exception:
                                logging.getLogger("VoiceHandler").exception(
                                    "Failed to finalize fallback WAV for user %s",
                                    user,
                                )
                        self._release_capture_id(capture_id)
                    if self.callback:
                        await self.callback(user, voice_data, result_id, capture_id)
                elif event_name == "discard":
                    if capture_id:
                        await self._abort_core_stream(capture_id)
                        await self._abort_fallback_capture(capture_id)
                        self._release_capture_id(capture_id)
                    if self.callback:
                        await self.callback(user, None, None, capture_id)
                elif event_name == "abort":
                    if capture_id:
                        await self._abort_core_stream(capture_id)
                        await self._abort_fallback_capture(capture_id)
                        self._release_capture_id(capture_id)
            except asyncio.CancelledError:
                if capture_id:
                    await self._abort_core_stream(capture_id)
                    if event_name == "finish":
                        voice_data = None
                        if self.on_capture_finish_callback:
                            with suppress(Exception):
                                voice_data = await self.on_capture_finish_callback(
                                    capture_id
                                )
                        self._release_capture_id(capture_id)
                        if self.callback:
                            with suppress(Exception):
                                await self.callback(user, voice_data, None, capture_id)
                    else:
                        await self._abort_fallback_capture(capture_id)
                        self._release_capture_id(capture_id)
                        if self.callback:
                            with suppress(Exception):
                                await self.callback(user, None, None, capture_id)
                raise
            except Exception:
                logging.getLogger("VoiceHandler").exception(
                    "Voice segment event failed: event=%s user=%s",
                    event_name,
                    user,
                )
                if capture_id:
                    await self._abort_core_stream(capture_id)
                    if event_name in {"finish", "discard", "abort"}:
                        await self._abort_fallback_capture(capture_id)
                        self._release_capture_id(capture_id)
                    elif event_name in {"start", "audio"}:
                        self._mark_stream_failed(capture_id)
                if event_name in {"finish", "discard"} and self.callback:
                    with suppress(Exception):
                        await self.callback(user, None, None, capture_id)
            finally:
                self._processing_event = None
                self._event_queue.task_done()

        for capture_id in list(self._started_streams):
            await self._abort_core_stream(capture_id)
        self._started_streams.clear()
        with self._state_lock:
            active_captures = list(self._active_capture_ids)
        for capture_id in active_captures:
            await self._abort_fallback_capture(capture_id)
            self._release_capture_id(capture_id)

    async def _abort_core_stream(self, capture_id: str) -> None:
        if capture_id in self._started_streams and self.on_stream_abort_callback:
            with suppress(Exception):
                await self.on_stream_abort_callback(capture_id)
        self._started_streams.discard(capture_id)
        self._clear_stream_failure(capture_id)

    def _mark_stream_failed(self, capture_id: str) -> None:
        with self._event_reservation_lock:
            self._failed_streams.add(capture_id)

    async def _abort_fallback_capture(self, capture_id: str) -> None:
        if self.on_capture_abort_callback:
            with suppress(Exception):
                await self.on_capture_abort_callback(capture_id)

    def _release_capture_id(self, capture_id: str) -> None:
        with self._state_lock:
            self._active_capture_ids.discard(capture_id)
            self._stream_audio_buffers.pop(capture_id, None)
        self._clear_stream_failure(capture_id)


class VoiceHandler:
    """Collect finalized Discord PCM and encode it for Core perception.

    This class deliberately has no speech-recognition dependency.  Core receives the WAV
    segment and selects local/remote perception according to its runtime
    profile.
    """

    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.enabled = bool(config.voice.enabled)
        self.sample_rate = int(config.voice.sample_rate or DISCORD_SAMPLE_RATE)
        self._max_pcm_bytes = int(
            self.sample_rate
            * DISCORD_CHANNELS
            * DISCORD_WIDTH
            * MAX_UTTERANCE_SECONDS
        )
        self._streams: dict[str, bytearray] = {}
        self._stream_lock = threading.RLock()
        if not self.enabled:
            self.logger.info("Discord voice capture disabled by configuration")

    @property
    def supports_streaming(self) -> bool:
        # Kept for the existing Sink callback contract; it now means that the
        # adapter can collect voice, not that it owns a streaming recognizer.
        return self.enabled

    def start_stream(self, stream_id: str) -> bool:
        if not self.supports_streaming:
            return False
        with self._stream_lock:
            if (
                stream_id not in self._streams
                and len(self._streams) >= MAX_ACTIVE_UTTERANCES
            ):
                return False
            self._streams[stream_id] = bytearray()
        return True

    def accept_pcm(self, stream_id: str, pcm_data: bytes) -> None:
        if not self.supports_streaming:
            return
        if not pcm_data:
            return
        with self._stream_lock:
            stream = self._streams.setdefault(stream_id, bytearray())
            remaining = self._max_pcm_bytes - len(stream)
            if remaining <= 0:
                return
            # Keep the beginning of the utterance when a sender exceeds the
            # bound; this avoids unbounded memory while preserving context.
            stream.extend(bytes(pcm_data[:remaining]))

    def finish_stream(self, stream_id: str) -> Optional[str]:
        if not self.supports_streaming:
            return None
        with self._stream_lock:
            pcm_data = bytes(self._streams.pop(stream_id, bytearray()))
        if not pcm_data:
            return None
        try:
            return pcm16_to_wav_base64(
                pcm_data,
                sample_rate=self.sample_rate,
                channels=DISCORD_CHANNELS,
                max_duration_seconds=MAX_UTTERANCE_SECONDS,
            ) or None
        except Exception:
            self.logger.exception("Failed to encode Discord voice segment")
            return None

    def abort_stream(self, stream_id: str) -> None:
        with self._stream_lock:
            self._streams.pop(stream_id, None)
