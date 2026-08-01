"""Discord voice capture, VAD, and incremental speech recognition."""

import asyncio
import logging
import threading
import time
from collections import deque
from contextlib import suppress
from typing import Callable, Optional

import numpy as np
from config import AdapterConfig, VoiceConfig
from discord.sinks import Filters, Sink
from multimodal_bridge import ensure_multimodal_import
from scipy.signal import resample_poly


ensure_multimodal_import()

from nachobot_multimodal.asr.streaming import StreamingASR  # noqa: E402


# Discord sends stereo 48 kHz, signed 16-bit PCM.
DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
DISCORD_WIDTH = 2
ASR_SAMPLE_RATE = 16000


class SilenceDetectingSink(Sink):
    """Turn Discord PCM packets into ordered streaming-ASR lifecycle events."""

    ASR_PREROLL_SECONDS = 0.3

    def __init__(
        self,
        filters=None,
        callback: Optional[Callable] = None,
        on_speech_start_callback: Optional[Callable] = None,
        on_stream_start_callback: Optional[Callable] = None,
        on_stream_audio_callback: Optional[Callable] = None,
        on_stream_finish_callback: Optional[Callable] = None,
        on_stream_abort_callback: Optional[Callable] = None,
        config: Optional[VoiceConfig] = None,
    ):
        super().__init__(filters=filters)

        # All callbacks are async and run on the Discord event loop. The audio
        # callback only queues events, so sherpa decoding never blocks py-cord's
        # DecodeManager thread.
        self.callback = callback
        self.on_speech_start_callback = on_speech_start_callback
        self.on_stream_start_callback = on_stream_start_callback
        self.on_stream_audio_callback = on_stream_audio_callback
        self.on_stream_finish_callback = on_stream_finish_callback
        self.on_stream_abort_callback = on_stream_abort_callback
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

        self.vad_threshold = config.vad_threshold if config else 500
        self.silence_threshold = config.silence_threshold if config else 0.5
        self.min_speech_duration = 0.3
        self._bytes_per_second = (
            DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * DISCORD_WIDTH
        )
        self._max_preroll_bytes = int(
            self._bytes_per_second * self.ASR_PREROLL_SECONDS
        )

        self._state_lock = threading.RLock()
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._started_streams: set[int] = set()
        self.checker_task: Optional[asyncio.Task] = None
        self.stream_worker_task: Optional[asyncio.Task] = None
        self._stopped = False
        self._shutdown_queued = False

        logging.getLogger("VoiceHandler").info(
            "SilenceDetectingSink initialized with incremental ASR"
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

    def _begin_shutdown(self) -> None:
        if self._shutdown_queued:
            return
        self._shutdown_queued = True
        self._ensure_tasks()

        if self.checker_task:
            self.checker_task.cancel()

        with self._state_lock:
            active_users = [
                user
                for user, speaking in self.is_speaking.items()
                if speaking
            ]
            for user in active_users:
                self.is_speaking[user] = False

        for user in active_users:
            self._event_queue.put_nowait(("abort", user, None))
        self._event_queue.put_nowait(("stop", None, None))

    def _append_preroll(self, user: int, pcm_data: bytes) -> None:
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

    def _queue_from_audio_thread(
        self,
        event: tuple[str, int, Optional[bytes]],
    ) -> None:
        if self._stopped or not self.loop or not self.loop.is_running():
            return
        self.loop.call_soon_threadsafe(self._event_queue.put_nowait, event)

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
        events: list[tuple[str, int, Optional[bytes]]] = []
        notify_speech_start = False

        with self._state_lock:
            speaking = self.is_speaking.setdefault(user, False)

            if not speaking:
                self._append_preroll(user, pcm_data)

            if is_speech:
                self.last_speech_time[user] = now
                if not speaking:
                    self.is_speaking[user] = True
                    notify_speech_start = True
                    preroll = self._take_preroll(user)
                    self.utterance_bytes[user] = sum(map(len, preroll))
                    self.voiced_bytes[user] = len(pcm_data)
                    events.append(("start", user, None))
                    events.extend(("audio", user, chunk) for chunk in preroll)
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
                    events.append(("audio", user, pcm_data))
            elif speaking:
                # Keep feeding trailing silence until VAD closes the stream.
                self.utterance_bytes[user] = (
                    self.utterance_bytes.get(user, 0) + len(pcm_data)
                )
                events.append(("audio", user, pcm_data))

        for event in events:
            self._queue_from_audio_thread(event)

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
                completed: list[tuple[str, int, Optional[bytes]]] = []

                with self._state_lock:
                    for user, speaking in list(self.is_speaking.items()):
                        if not speaking:
                            continue
                        silence_duration = now - self.last_speech_time.get(user, 0)
                        if silence_duration <= self.silence_threshold:
                            continue

                        self.is_speaking[user] = False
                        total_duration = (
                            self.utterance_bytes.pop(user, 0)
                            / self._bytes_per_second
                        )
                        voiced_duration = (
                            self.voiced_bytes.pop(user, 0)
                            / self._bytes_per_second
                        )
                        self.last_speech_time.pop(user, None)

                        event_name = (
                            "finish"
                            if voiced_duration >= self.min_speech_duration
                            else "discard"
                        )
                        completed.append((event_name, user, None))
                        logging.getLogger("VoiceHandler").info(
                            "Discord speech ended for %s: %.2fs total, "
                            "%.2fs voiced",
                            user,
                            total_duration,
                            voiced_duration,
                        )

                for event in completed:
                    self._event_queue.put_nowait(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger("VoiceHandler").exception(
                    "Silence checker error"
                )
                await asyncio.sleep(0.2)

    async def _stream_event_worker(self) -> None:
        """Run ordered ASR operations without blocking the capture thread."""
        while True:
            event_name, user, pcm_data = await self._event_queue.get()
            try:
                if event_name == "stop":
                    break

                if event_name == "start":
                    started = bool(
                        self.on_stream_start_callback
                        and await self.on_stream_start_callback(user)
                    )
                    if started:
                        self._started_streams.add(user)
                elif event_name == "audio":
                    if (
                        user in self._started_streams
                        and self.on_stream_audio_callback
                        and pcm_data
                    ):
                        await self.on_stream_audio_callback(user, pcm_data)
                elif event_name == "finish":
                    text = None
                    try:
                        if (
                            user in self._started_streams
                            and self.on_stream_finish_callback
                        ):
                            text = await self.on_stream_finish_callback(user)
                    except Exception:
                        logging.getLogger("VoiceHandler").exception(
                            "Failed to finalize streaming ASR for user %s",
                            user,
                        )
                    finally:
                        self._started_streams.discard(user)
                        if self.callback:
                            await self.callback(user, text)
                elif event_name == "discard":
                    try:
                        if (
                            user in self._started_streams
                            and self.on_stream_abort_callback
                        ):
                            await self.on_stream_abort_callback(user)
                    finally:
                        self._started_streams.discard(user)
                        if self.callback:
                            await self.callback(user, None)
                elif event_name == "abort":
                    if (
                        user in self._started_streams
                        and self.on_stream_abort_callback
                    ):
                        await self.on_stream_abort_callback(user)
                    self._started_streams.discard(user)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger("VoiceHandler").exception(
                    "Streaming ASR event failed: event=%s user=%s",
                    event_name,
                    user,
                )
                self._started_streams.discard(user)
            finally:
                self._event_queue.task_done()

        # The stop marker is queued after per-user aborts, but guard against a
        # partially initialized stream if a callback raised.
        if self.on_stream_abort_callback:
            for user in list(self._started_streams):
                with suppress(Exception):
                    await self.on_stream_abort_callback(user)
        self._started_streams.clear()


class VoiceHandler:
    """Convert Discord PCM chunks and drive Multimodal's shared ASR engine."""

    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.asr: Optional[StreamingASR] = None

        if not config.voice.enabled:
            self.logger.info("Discord streaming ASR disabled by configuration")
            return

        try:
            self.asr = StreamingASR(logger=logger)
            if not self.asr.supports_streaming:
                self.logger.error(
                    "Discord streaming ASR unavailable; check Multimodal ASR "
                    "model and CPU runtime"
                )
        except Exception:
            self.logger.exception("Failed to initialize Discord streaming ASR")
            self.asr = None

    @property
    def supports_streaming(self) -> bool:
        return self.asr is not None and self.asr.supports_streaming

    def start_stream(self, stream_id: str) -> bool:
        if not self.supports_streaming:
            return False
        return self.asr.start_stream(stream_id)

    @staticmethod
    def _pcm_to_16k(pcm_data: bytes) -> np.ndarray:
        frame_width = DISCORD_CHANNELS * DISCORD_WIDTH
        usable = len(pcm_data) - (len(pcm_data) % frame_width)
        if usable <= 0:
            return np.empty(0, dtype=np.float32)

        samples = np.frombuffer(pcm_data[:usable], dtype=np.int16)
        stereo = samples.reshape(-1, DISCORD_CHANNELS).astype(np.float32)
        mono = stereo.mean(axis=1) / 32768.0
        mono_16k = resample_poly(
            mono,
            ASR_SAMPLE_RATE,
            DISCORD_SAMPLE_RATE,
        )
        return np.ascontiguousarray(mono_16k, dtype=np.float32)

    def accept_pcm(self, stream_id: str, pcm_data: bytes) -> Optional[str]:
        if not self.supports_streaming:
            return None
        try:
            samples_16k = self._pcm_to_16k(pcm_data)
            return self.asr.accept_stream_audio(stream_id, samples_16k)
        except Exception:
            self.logger.exception(
                "Failed to feed Discord PCM into streaming ASR: %s",
                stream_id,
            )
            self.abort_stream(stream_id)
            return None

    def finish_stream(self, stream_id: str) -> Optional[str]:
        if not self.supports_streaming:
            return None
        text = self.asr.finish_stream(stream_id)
        if not text:
            return None
        text = "".join(
            character
            for character in text.strip()
            if character.isprintable() or character in "\n\r\t"
        )
        if text:
            self.logger.info("Discord ASR recognized: %s", text)
            return text
        return None

    def abort_stream(self, stream_id: str) -> None:
        if self.asr is not None:
            self.asr.abort_stream(stream_id)
