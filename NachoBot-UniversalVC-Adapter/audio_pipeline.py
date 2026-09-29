"""UniversalVC capture pipeline: Denoise → VAD → Speaker ID → Core voice."""

import asyncio
import concurrent.futures
import logging
import queue
import threading
from typing import Callable, Optional

import numpy as np
from scipy.signal import resample_poly

from config import AdapterConfig
from denoise import DenoiseProcessor
from vad_processor import VADProcessor
from speaker_tracker import SpeakerTracker
from voice_codec import samples_to_wav_base64


class _StreamContext:
    """Capture-thread state for one VAD segment's Core streaming request."""

    def __init__(self, key: str):
        self.key = key
        self.receipt: concurrent.futures.Future = concurrent.futures.Future()
        self.failed = threading.Event()
        self.pending_pcm = bytearray()
        self.samples_queued = 0
        self.sequence = 0

    def fail(self) -> None:
        self.failed.set()
        self.resolve(None)

    def resolve(self, stream_result: Optional[tuple[str, str]]) -> None:
        if not self.receipt.done():
            try:
                self.receipt.set_result(stream_result)
            except concurrent.futures.InvalidStateError:
                pass


class _StreamCaptureState:
    """Small rolling pre-roll buffer and current stream for one capture source."""

    def __init__(self):
        self.pre_roll = bytearray()
        self.active: Optional[_StreamContext] = None


class AudioPipeline:
    """Real-time audio processing pipeline."""

    # Target sample rate for VAD / speaker embedding / Core voice payload.
    TARGET_SR = 16000
    MAX_UTTERANCE_SECONDS = 60.0
    STREAM_PRE_ROLL_MS = 400
    STREAM_CHUNK_MS = 160
    MAX_STREAM_QUEUE_EVENTS = 32

    def __init__(
        self,
        config: AdapterConfig,
        logger: logging.Logger,
        on_result: Optional[Callable] = None,
        on_speech_start: Optional[Callable] = None,
        on_mic_speech_start: Optional[Callable] = None,
        on_mic_speech_end: Optional[Callable] = None,
        stream_client=None,
    ):
        """
        Args:
            config: Full adapter configuration.
            on_result: async callback(
                speaker_id, speaker_name, voice_base64, result_id, confirmed_text
            )
            on_speech_start: async callback() when speech starts (for playback interruption)
            on_mic_speech_start: async callback() when mic speech starts (for playback pausing)
            on_mic_speech_end: async callback() when mic speech ends (for playback resuming)
            stream_client: async Core audio-stream transport, or None to disable streaming
        """
        self.logger = logger
        self.on_result = on_result
        self.on_speech_start = on_speech_start
        self.on_mic_speech_start = on_mic_speech_start
        self.on_mic_speech_end = on_mic_speech_end
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.stream_client = stream_client
        self._stream_queue = queue.Queue(maxsize=self.MAX_STREAM_QUEUE_EVENTS)
        self._stream_wakeup: Optional[asyncio.Event] = None
        self._stream_sender_task: Optional[asyncio.Task] = None
        self._stream_shutdown_requested = False
        self._stream_contexts = {}
        self._stream_ids = {}
        self._stream_counter = 0
        self._stream_counter_lock = threading.Lock()
        self._speaker_id_lock = threading.Lock()
        self._segment_jobs = set()
        self._segment_jobs_lock = threading.Lock()
        self._main_stream_state = _StreamCaptureState()
        self._mic_stream_state = _StreamCaptureState()
        self._speech_started_notified = False
        self._frame_counter = 0

        # ── Stage 1: Denoiser (operates at 48kHz) ──
        self.denoiser = DenoiseProcessor(
            enabled=config.denoise.enabled,
            logger=logger,
        )

        # ── Stage 2: VAD (operates at 16kHz) ──
        self.vad = VADProcessor(
            model_path=config.vad.model_path,
            threshold=config.vad.threshold,
            min_silence_duration=config.vad.min_silence_duration,
            min_speech_duration=config.vad.min_speech_duration,
            logger=logger,
        )

        # ── Stage 2.5: Mic VAD (Independent state for microphone) ──
        if config.microphone.enabled:
            self.mic_vad = VADProcessor(
                model_path=config.vad.model_path,
                threshold=config.vad.threshold,
                min_silence_duration=config.vad.min_silence_duration,
                min_speech_duration=config.vad.min_speech_duration,
                logger=logger,
            )
            self._owner_id = config.microphone.owner_speaker_id
            self._owner_name = config.microphone.owner_speaker_name
            self._mic_speech_started_notified = False

        # ── Stage 3: Speaker Tracker ──
        self.speaker_tracker = SpeakerTracker(
            enabled=config.speaker.enabled,
            embedding_model_path=config.speaker.embedding_model_path,
            similarity_threshold=config.speaker.similarity_threshold,
            max_speakers=config.speaker.max_speakers,
            db_path=config.speaker.db_path,
            logger=logger,
        )

        self.logger.info(
            "AudioPipeline initialized: "
            f"denoise={config.denoise.enabled}, vad=Silero, "
            f"speaker={config.speaker.enabled}, perception=Core"
        )

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        if self.stream_client is not None and self._stream_sender_task is None:
            self._stream_wakeup = asyncio.Event()
            self._stream_sender_task = loop.create_task(
                self._run_stream_sender(), name="universalvc-core-audio-stream"
            )

    def process_frame(self, pcm_float32: bytes, sample_rate: int, channels: int):
        """Process a raw audio frame through the full pipeline.
        Called from the audio capture thread. This method must be fast
        and non-blocking.
        """
        try:
            # Accept both raw bytes and numpy arrays from capture backends
            if isinstance(pcm_float32, (bytes, bytearray)):
                samples = np.frombuffer(pcm_float32, dtype=np.float32)
            elif isinstance(pcm_float32, np.ndarray):
                samples = pcm_float32.astype(np.float32, copy=False)
            else:
                samples = np.array(pcm_float32, dtype=np.float32)

            # Ensure 1D
            samples = samples.ravel()
            if len(samples) == 0:
                return

            # 1. Mix to mono
            if channels > 1:
                if len(samples) % channels != 0:
                    samples = samples[: len(samples) - (len(samples) % channels)]
                samples = samples.reshape(-1, channels).mean(axis=1)

            # 流水心跳诊断：确认是否有真实的音频信号送达 Pipeline 以及它的音量幅值
            self._frame_counter += 1
            if self._frame_counter % 100 == 0:
                rms = float(np.sqrt(np.mean(samples**2)))
                self.logger.debug(
                    f"[Pipeline Heartbeat] 已处理 100 帧. 当前帧大小: {len(samples)} 采样点, RMS 幅值: {rms:.5f} (对应 int16 精度: {int(rms * 32767)})"
                )

            # 2. Downsample to 16kHz for VAD
            if sample_rate != self.TARGET_SR:
                mono_16k = self._resample(samples, sample_rate, self.TARGET_SR)
            else:
                mono_16k = samples

            # Ensure 1D contiguous float32 for sherpa-onnx VAD
            mono_16k = np.ascontiguousarray(mono_16k.ravel(), dtype=np.float32)

            # 4. Feed VAD.  Recognition is intentionally deferred to Core.
            segments = self.vad.feed(mono_16k)
            is_speaking = self.vad.is_speaking
            receipts = self._advance_stream_capture(
                self._main_stream_state,
                mono_16k,
                is_speaking=is_speaking,
                completed_segment_audio=[
                    np.asarray(seg.samples).size > 0 for seg in segments
                ],
            )

            # Notify speech start (for playback interruption)
            if is_speaking and not self._speech_started_notified:
                self._speech_started_notified = True
                if self.on_speech_start and self._loop:
                    asyncio.run_coroutine_threadsafe(self.on_speech_start(), self._loop)

            if not is_speaking:
                self._speech_started_notified = False

            # 5. Speaker identification uses completed VAD segments; Core
            # receives the same bounded segment for local/remote perception.
            for index, seg in enumerate(segments):
                if self._loop:
                    self._schedule_segment(
                        self._process_segment(
                            seg.samples,
                            receipts[index] if index < len(receipts) else None,
                        )
                    )
        except Exception as e:
            self.logger.error(f"Pipeline frame error: {e}")

    def process_mic_frame(self, pcm_float32: bytes, sample_rate: int, channels: int):
        """Process a raw audio frame from the microphone."""
        try:
            if isinstance(pcm_float32, (bytes, bytearray)):
                samples = np.frombuffer(pcm_float32, dtype=np.float32)
            elif isinstance(pcm_float32, np.ndarray):
                samples = pcm_float32.astype(np.float32, copy=False)
            else:
                samples = np.array(pcm_float32, dtype=np.float32)

            samples = samples.ravel()
            if len(samples) == 0:
                return

            if channels > 1:
                if len(samples) % channels != 0:
                    samples = samples[: len(samples) - (len(samples) % channels)]
                samples = samples.reshape(-1, channels).mean(axis=1)

            if sample_rate != self.TARGET_SR:
                mono_16k = self._resample(samples, sample_rate, self.TARGET_SR)
            else:
                mono_16k = samples

            mono_16k = np.ascontiguousarray(mono_16k.ravel(), dtype=np.float32)

            segments = self.mic_vad.feed(mono_16k)
            is_speaking = self.mic_vad.is_speaking
            receipts = self._advance_stream_capture(
                self._mic_stream_state,
                mono_16k,
                is_speaking=is_speaking,
                completed_segment_audio=[
                    np.asarray(seg.samples).size > 0 for seg in segments
                ],
            )

            if is_speaking and not self._mic_speech_started_notified:
                self._mic_speech_started_notified = True
                if self.on_mic_speech_start and self._loop:
                    asyncio.run_coroutine_threadsafe(self.on_mic_speech_start(), self._loop)

            if not is_speaking and self._mic_speech_started_notified:
                self._mic_speech_started_notified = False
                if self.on_mic_speech_end and self._loop:
                    asyncio.run_coroutine_threadsafe(self.on_mic_speech_end(), self._loop)

            for index, seg in enumerate(segments):
                if self._loop:
                    self._schedule_segment(
                        self._process_mic_segment(
                            seg.samples,
                            receipts[index] if index < len(receipts) else None,
                        )
                    )
        except Exception as e:
            self.logger.error(f"Mic pipeline frame error: {e}")

    def _schedule_segment(self, coroutine) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            coroutine.close()
            return
        try:
            job = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            coroutine.close()
            return
        with self._segment_jobs_lock:
            self._segment_jobs.add(job)
        job.add_done_callback(self._discard_segment_job)

    def _discard_segment_job(self, job: concurrent.futures.Future) -> None:
        with self._segment_jobs_lock:
            self._segment_jobs.discard(job)

    def _advance_stream_capture(
        self,
        state: _StreamCaptureState,
        samples_16k: np.ndarray,
        *,
        is_speaking: bool,
        completed_segment_audio: list[bool],
    ) -> list[Optional[concurrent.futures.Future]]:
        """Queue bounded PCM events without waiting on Core from a capture thread."""
        receipts: list[Optional[concurrent.futures.Future]] = [
            None for _ in completed_segment_audio
        ]
        if self.stream_client is None or self._loop is None:
            return receipts

        pcm = self._float_to_s16le(samples_16k)
        context = state.active

        # A VAD segment is the only authority for finishing a stream. If a
        # backend emits multiple segments for one frame, the active stream can
        # be associated with only one of them; leave all ambiguous receipts
        # empty so those WAVs take Core's normal full-audio path.
        if context is not None and completed_segment_audio:
            valid_indexes = [
                index for index, has_audio in enumerate(completed_segment_audio)
                if has_audio
            ]
            state.active = None
            if len(completed_segment_audio) == 1 and valid_indexes:
                self._flush_stream_audio(context, force=True)
                if not context.failed.is_set():
                    self._enqueue_stream_event("finish", context)
                else:
                    self._enqueue_stream_event("abort", context)
                receipts[valid_indexes[0]] = context.receipt
            else:
                self._enqueue_stream_event("abort", context)
            context = None
        elif context is not None and not is_speaking:
            # VAD ended without a valid segment (for example, noise shorter
            # than min_speech_duration). Do not leave a Core stream open.
            state.active = None
            self._enqueue_stream_event("abort", context)
            context = None

        if is_speaking:
            if context is None:
                context = self._new_stream_context()
                state.active = context
                pre_roll = bytes(state.pre_roll)
                context.samples_queued = len(pre_roll) // 2
                self._enqueue_stream_event("start", context, pre_roll)
            self._append_stream_audio(context, pcm)

        self._append_pre_roll(state, pcm)
        return receipts

    def _new_stream_context(self) -> _StreamContext:
        with self._stream_counter_lock:
            self._stream_counter += 1
            key = f"universalvc-{self._stream_counter}"
        return _StreamContext(key)

    @staticmethod
    def _float_to_s16le(samples_16k: np.ndarray) -> bytes:
        samples = np.asarray(samples_16k, dtype=np.float32).reshape(-1)
        samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
        pcm = np.rint(np.clip(samples, -1.0, 1.0) * 32767.0)
        return pcm.astype("<i2", copy=False).tobytes()

    def _append_pre_roll(self, state: _StreamCaptureState, pcm: bytes) -> None:
        max_bytes = self.TARGET_SR * 2 * self.STREAM_PRE_ROLL_MS // 1000
        state.pre_roll.extend(pcm)
        if len(state.pre_roll) > max_bytes:
            del state.pre_roll[: len(state.pre_roll) - max_bytes]

    def _append_stream_audio(self, context: _StreamContext, pcm: bytes) -> None:
        if context.failed.is_set() or not pcm:
            return
        samples = len(pcm) // 2
        max_samples = int(self.TARGET_SR * self.MAX_UTTERANCE_SECONDS)
        if context.samples_queued + samples > max_samples:
            self.logger.warning(
                "Core audio stream exceeded %.1fs; using the bounded full-WAV path",
                self.MAX_UTTERANCE_SECONDS,
            )
            context.fail()
            self._enqueue_stream_event("abort", context)
            return
        context.samples_queued += samples
        context.pending_pcm.extend(pcm)
        self._flush_stream_audio(context)

    def _flush_stream_audio(self, context: _StreamContext, *, force: bool = False) -> None:
        chunk_bytes = self.TARGET_SR * 2 * self.STREAM_CHUNK_MS // 1000
        while context.pending_pcm and (
            len(context.pending_pcm) >= chunk_bytes or force
        ):
            count = min(chunk_bytes, len(context.pending_pcm))
            chunk = bytes(context.pending_pcm[:count])
            del context.pending_pcm[:count]
            if not self._enqueue_stream_event("chunk", context, chunk):
                context.pending_pcm.clear()
                return

    def _enqueue_stream_event(
        self, kind: str, context: _StreamContext, pcm: bytes = b""
    ) -> bool:
        if context.failed.is_set() and kind not in {"abort"}:
            return False
        try:
            self._stream_queue.put_nowait((kind, context, pcm))
        except queue.Full:
            context.fail()
            self.logger.warning(
                "Core audio stream queue is full; using the bounded full-WAV path"
            )
            return False

        loop = self._loop
        wakeup = self._stream_wakeup
        if loop is None or wakeup is None or loop.is_closed():
            context.fail()
            return False
        try:
            loop.call_soon_threadsafe(wakeup.set)
        except RuntimeError:
            context.fail()
            return False
        return True

    async def _next_stream_event(self):
        wakeup = self._stream_wakeup
        if wakeup is None:
            return None
        while not self._stream_shutdown_requested:
            try:
                return self._stream_queue.get_nowait()
            except queue.Empty:
                wakeup.clear()
                # Recheck after clearing so a concurrent capture-thread put
                # cannot get stranded before the event wait begins.
                try:
                    return self._stream_queue.get_nowait()
                except queue.Empty:
                    await wakeup.wait()
        return None

    async def _run_stream_sender(self) -> None:
        try:
            while not self._stream_shutdown_requested:
                event = await self._next_stream_event()
                if event is None:
                    continue
                kind, context, pcm = event
                await self._handle_stream_event(kind, context, pcm)
                await self._abort_failed_streams()
        except asyncio.CancelledError:
            self._stream_shutdown_requested = True
            await self._abort_all_streams()
            raise
        except Exception:
            self.logger.exception("Core audio stream sender stopped unexpectedly")
            await self._abort_all_streams()
        finally:
            await self._abort_all_streams()
            self._fail_queued_streams()

    async def _handle_stream_event(
        self, kind: str, context: _StreamContext, pcm: bytes
    ) -> None:
        if kind == "start":
            await self._start_core_stream(context, pcm)
        elif kind == "chunk":
            await self._send_core_chunk(context, pcm)
        elif kind == "finish":
            await self._finish_core_stream(context)
        elif kind == "abort":
            await self._abort_core_stream(context)

    async def _start_core_stream(self, context: _StreamContext, pre_roll: bytes) -> None:
        self._stream_contexts[context.key] = context
        if context.failed.is_set():
            self._forget_stream(context)
            return
        try:
            stream_id = await self.stream_client.start_stream(
                sample_rate=self.TARGET_SR, channels=1
            )
            if not isinstance(stream_id, str) or not stream_id.strip():
                raise ValueError("Core returned an invalid stream ID")
            self._stream_ids[context.key] = stream_id
            if context.failed.is_set():
                await self._abort_core_stream(context)
                return
            chunk_bytes = self.TARGET_SR * 2 * self.STREAM_CHUNK_MS // 1000
            for offset in range(0, len(pre_roll), chunk_bytes):
                if context.failed.is_set():
                    await self._abort_core_stream(context)
                    return
                await self._send_core_pcm(
                    context,
                    stream_id,
                    pre_roll[offset : offset + chunk_bytes],
                )
            if context.failed.is_set():
                await self._abort_core_stream(context)
        except asyncio.CancelledError:
            context.fail()
            await self._abort_core_stream(context)
            raise
        except Exception as exc:
            self.logger.warning(
                "Core audio stream start failed (%s); using full-WAV ASR",
                type(exc).__name__,
            )
            context.fail()
            await self._abort_core_stream(context)

    async def _send_core_chunk(self, context: _StreamContext, pcm: bytes) -> None:
        if context.failed.is_set():
            await self._abort_core_stream(context)
            return
        stream_id = self._stream_ids.get(context.key)
        if stream_id is None:
            context.fail()
            self._forget_stream(context)
            return
        try:
            await self._send_core_pcm(context, stream_id, pcm)
        except asyncio.CancelledError:
            context.fail()
            await self._abort_core_stream(context)
            raise
        except Exception as exc:
            self.logger.warning(
                "Core audio stream chunk failed (%s); using full-WAV ASR",
                type(exc).__name__,
            )
            context.fail()
            await self._abort_core_stream(context)

    async def _send_core_pcm(
        self, context: _StreamContext, stream_id: str, pcm: bytes
    ) -> None:
        if not pcm:
            return
        await self.stream_client.send_chunk(stream_id, context.sequence, pcm)
        context.sequence += 1
        if context.failed.is_set():
            await self._abort_core_stream(context)

    async def _finish_core_stream(self, context: _StreamContext) -> None:
        if context.failed.is_set():
            await self._abort_core_stream(context)
            return
        stream_id = self._stream_ids.get(context.key)
        if stream_id is None:
            context.fail()
            self._forget_stream(context)
            return
        try:
            response = await self.stream_client.finish_stream(stream_id)
            stream_result = self._confirmed_stream_result(response)
            if context.failed.is_set():
                await self._abort_core_stream(context)
                return
            if not stream_result:
                self.logger.info(
                    "Core stream returned no confirmed transcript; using full-WAV ASR"
                )
            context.resolve(stream_result)
            self._forget_stream(context)
        except asyncio.CancelledError:
            context.fail()
            await self._abort_core_stream(context)
            raise
        except Exception as exc:
            self.logger.warning(
                "Core audio stream finish failed (%s); using full-WAV ASR",
                type(exc).__name__,
            )
            context.fail()
            await self._abort_core_stream(context)

    @staticmethod
    def _confirmed_stream_result(response) -> Optional[tuple[str, str]]:
        if not isinstance(response, dict):
            return None
        text = response.get("text")
        result_id = response.get("result_id")
        if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
            return None
        if not isinstance(result_id, str) or not result_id.strip() or len(result_id) > 256:
            return None
        return result_id.strip(), text.strip()

    async def _abort_core_stream(self, context: _StreamContext) -> None:
        context.fail()
        stream_id = self._stream_ids.get(context.key)
        if stream_id is not None:
            try:
                await self.stream_client.abort_stream(stream_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.debug(
                    "Best-effort Core stream abort failed (%s)", type(exc).__name__
                )
        self._forget_stream(context)

    async def _abort_failed_streams(self) -> None:
        for context in list(self._stream_contexts.values()):
            if context.failed.is_set():
                await self._abort_core_stream(context)

    async def _abort_all_streams(self) -> None:
        contexts = list(self._stream_contexts.values())
        for context in contexts:
            await self._abort_core_stream(context)

    def _forget_stream(self, context: _StreamContext) -> None:
        self._stream_ids.pop(context.key, None)
        self._stream_contexts.pop(context.key, None)
        context.resolve(None)

    def _fail_queued_streams(self) -> None:
        while True:
            try:
                _, context, _ = self._stream_queue.get_nowait()
            except queue.Empty:
                break
            context.fail()

    async def stop_streaming(self) -> None:
        """Stop the one sender task and abort any Core streams still open."""
        self._stream_shutdown_requested = True
        for state in (self._main_stream_state, self._mic_stream_state):
            if state.active is not None:
                state.active.fail()
                state.active = None
        self._fail_queued_streams()
        if self._stream_wakeup is not None:
            self._stream_wakeup.set()

        # Capture has stopped before this method is called. Cancel the finite
        # segment callbacks so shutdown cannot publish late full-WAV messages.
        with self._segment_jobs_lock:
            segment_jobs = list(self._segment_jobs)
        for job in segment_jobs:
            job.cancel()
        if segment_jobs:
            await asyncio.gather(
                *(asyncio.wrap_future(job) for job in segment_jobs),
                return_exceptions=True,
            )

        task = self._stream_sender_task
        if task is not None and task is not asyncio.current_task():
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _process_segment(
        self,
        samples_16k: np.ndarray,
        stream_receipt: Optional[concurrent.futures.Future] = None,
    ):
        """Finish speaker identification and publish a Core voice segment."""
        try:
            samples_16k = self._bound_samples(samples_16k)
            duration = len(samples_16k) / self.TARGET_SR
            self.logger.info(f"Processing speech segment: {duration:.2f}s")

            # Denoising is retained for speaker identification and the Core
            # voice payload.
            if self.denoiser.enabled:
                loop = asyncio.get_running_loop()
                samples_16k = await loop.run_in_executor(
                    None, self._denoise_segment, samples_16k
                )

            # 2. Speaker identification
            speaker_id, speaker_name = await asyncio.to_thread(
                self._identify_speaker, samples_16k
            )
            voice_data = samples_to_wav_base64(
                samples_16k,
                sample_rate=self.TARGET_SR,
                max_duration_seconds=self.MAX_UTTERANCE_SECONDS,
            )
            if voice_data and self.on_result:
                stream_result = await self._wait_for_stream_receipt(stream_receipt)
                self.logger.info(
                    "[%s] (%s): finalized voice segment (%d base64 chars)",
                    speaker_name,
                    speaker_id,
                    len(voice_data),
                )
                await self.on_result(
                    speaker_id,
                    speaker_name,
                    voice_data,
                    stream_result[0] if stream_result else None,
                    stream_result[1] if stream_result else None,
                )

        except Exception as e:
            self.logger.exception(f"Segment processing error: {e}")

    def _identify_speaker(self, samples_16k: np.ndarray):
        """Keep the shared tracker single-flight while keeping the loop responsive."""
        with self._speaker_id_lock:
            return self.speaker_tracker.identify(samples_16k)

    async def _process_mic_segment(
        self,
        samples_16k: np.ndarray,
        stream_receipt: Optional[concurrent.futures.Future] = None,
    ):
        """Publish a completed microphone voice segment with fixed owner ID."""
        try:
            samples_16k = self._bound_samples(samples_16k)
            duration = len(samples_16k) / self.TARGET_SR
            self.logger.info(f"Processing mic segment: {duration:.2f}s")

            if self.denoiser.enabled:
                loop = asyncio.get_running_loop()
                samples_16k = await loop.run_in_executor(
                    None, self._denoise_segment, samples_16k
                )

            # Bypass speaker identification for microphone, use fixed owner ID
            speaker_id = self._owner_id
            speaker_name = self._owner_name

            voice_data = samples_to_wav_base64(
                samples_16k,
                sample_rate=self.TARGET_SR,
                max_duration_seconds=self.MAX_UTTERANCE_SECONDS,
            )
            if voice_data and self.on_result:
                stream_result = await self._wait_for_stream_receipt(stream_receipt)
                self.logger.info(
                    "[%s] (%s) [Mic]: finalized voice segment (%d base64 chars)",
                    speaker_name,
                    speaker_id,
                    len(voice_data),
                )
                await self.on_result(
                    speaker_id,
                    speaker_name,
                    voice_data,
                    stream_result[0] if stream_result else None,
                    stream_result[1] if stream_result else None,
                )

        except Exception as e:
            self.logger.exception(f"Mic segment processing error: {e}")

    async def _wait_for_stream_receipt(
        self, receipt: Optional[concurrent.futures.Future]
    ) -> Optional[tuple[str, str]]:
        if receipt is None:
            return None
        try:
            stream_result = await asyncio.wrap_future(receipt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.debug(
                "Core stream receipt unavailable (%s); using full-WAV ASR",
                type(exc).__name__,
            )
            return None
        if (
            isinstance(stream_result, tuple)
            and len(stream_result) == 2
            and isinstance(stream_result[0], str)
            and stream_result[0].strip()
            and len(stream_result[0]) <= 256
            and isinstance(stream_result[1], str)
            and stream_result[1].strip()
            and len(stream_result[1]) <= 10_000
        ):
            return stream_result[0].strip(), stream_result[1].strip()
        return None

    def _bound_samples(self, samples_16k: np.ndarray) -> np.ndarray:
        """Bound a VAD result before denoise/encoding and Core transport."""
        samples = np.ascontiguousarray(samples_16k, dtype=np.float32).reshape(-1)
        max_samples = int(self.TARGET_SR * self.MAX_UTTERANCE_SECONDS)
        if samples.size > max_samples:
            self.logger.warning(
                "Speech segment exceeded %.1fs; truncating before Core transport",
                self.MAX_UTTERANCE_SECONDS,
            )
            samples = samples[:max_samples]
        return samples

    def _denoise_segment(self, samples_16k: np.ndarray) -> np.ndarray:
        """Upsample to 48kHz, denoise, and downsample to 16kHz."""
        try:
            samples_48k = self._resample(samples_16k, self.TARGET_SR, 48000)
            denoised_48k = self.denoiser.process(samples_48k)
            return self._resample(denoised_48k, 48000, self.TARGET_SR)
        except Exception as e:
            self.logger.error(f"Denoise segment error: {e}")
            return samples_16k

    @staticmethod
    def _resample(audio: np.ndarray, src_sr: int, tgt_sr: int) -> np.ndarray:
        """Resample audio cleanly without stateful window boundary artifacts."""
        if src_sr == 48000 and tgt_sr == 16000:
            if len(audio) % 3 != 0:
                audio = audio[: len(audio) - (len(audio) % 3)]
            return (audio[0::3] + audio[1::3] + audio[2::3]) / 3.0

        from math import gcd

        g = gcd(src_sr, tgt_sr)
        up = tgt_sr // g
        down = src_sr // g
        return resample_poly(audio, up, down).astype(np.float32)
