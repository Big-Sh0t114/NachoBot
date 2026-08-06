"""
Audio Pipeline — Unified real-time processing: Denoise → VAD → Speaker ID → ASR.

Receives raw float32 PCM frames from AudioCapture and orchestrates the full
processing chain, emitting (speaker_id, speaker_name, text) results via callback.
"""

import asyncio
from collections import deque
import logging
from typing import Callable, Deque, Dict, Optional

import numpy as np
from scipy.signal import resample_poly

from multimodal_bridge import ensure_multimodal_import

ensure_multimodal_import()

from nachobot_multimodal.asr.streaming import StreamingASR  # noqa: E402

from config import AdapterConfig
from denoise import DenoiseProcessor
from vad_processor import VADProcessor
from speaker_tracker import SpeakerTracker


class AudioPipeline:
    """Real-time audio processing pipeline."""

    # Target sample rate for VAD / ASR / Speaker embedding
    TARGET_SR = 16000
    ASR_PREROLL_SECONDS = 0.3
    SYSTEM_STREAM_ID = "system"
    MIC_STREAM_ID = "microphone"

    def __init__(
        self,
        config: AdapterConfig,
        logger: logging.Logger,
        on_result: Optional[Callable] = None,
        on_speech_start: Optional[Callable] = None,
        on_mic_speech_start: Optional[Callable] = None,
        on_mic_speech_end: Optional[Callable] = None,
    ):
        """
        Args:
            config: Full adapter configuration.
            on_result: async callback(speaker_id: str, speaker_name: str, text: str)
            on_speech_start: async callback() when speech starts (for TTS interruption)
            on_mic_speech_start: async callback() when mic speech starts (for TTS pausing)
            on_mic_speech_end: async callback() when mic speech ends (for TTS resuming)
        """
        self.logger = logger
        self.on_result = on_result
        self.on_speech_start = on_speech_start
        self.on_mic_speech_start = on_mic_speech_start
        self.on_mic_speech_end = on_mic_speech_end
        self._loop: Optional[asyncio.AbstractEventLoop] = None
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

        # ── Stage 4: ASR ──
        # Model, provider and thread settings are owned by Multimodal-Adapter.
        asr_kwargs = {"logger": logger}
        # Pass remote API config as fallback
        if config.stt.enabled:
            asr_kwargs["api_key"] = config.stt.api_key
            asr_kwargs["base_url"] = config.stt.base_url
            asr_kwargs["model"] = config.stt.model
        self.asr = StreamingASR(**asr_kwargs)

        self._asr_active: Dict[str, bool] = {
            self.SYSTEM_STREAM_ID: False,
            self.MIC_STREAM_ID: False,
        }
        self._asr_preroll: Dict[str, Deque[np.ndarray]] = {
            self.SYSTEM_STREAM_ID: deque(),
            self.MIC_STREAM_ID: deque(),
        }
        self._asr_preroll_samples: Dict[str, int] = {
            self.SYSTEM_STREAM_ID: 0,
            self.MIC_STREAM_ID: 0,
        }

        self.logger.info(
            "AudioPipeline initialized: "
            f"denoise={config.denoise.enabled}, vad=Silero, "
            f"speaker={config.speaker.enabled}, asr_mode={self.asr.mode}, "
            f"asr_provider={self.asr.provider}"
        )

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

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

            # 4. Feed VAD and incrementally decode while speech is in progress
            was_speaking = self.vad.is_speaking
            segments = self.vad.feed(mono_16k)
            is_speaking = self.vad.is_speaking
            streaming_text = self._feed_streaming_asr(
                self.SYSTEM_STREAM_ID,
                mono_16k,
                was_speaking,
                is_speaking,
            )

            # Notify speech start (for TTS interruption)
            if is_speaking and not self._speech_started_notified:
                self._speech_started_notified = True
                if self.on_speech_start and self._loop:
                    asyncio.run_coroutine_threadsafe(self.on_speech_start(), self._loop)

            if not is_speaking:
                self._speech_started_notified = False

            # 5. Speaker identification uses the completed VAD segment. ASR has
            # already been decoded incrementally, so no second decode is needed.
            for index, seg in enumerate(segments):
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._process_segment(
                            seg.samples,
                            streaming_text if index == 0 else None,
                        ),
                        self._loop,
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

            was_speaking = self.mic_vad.is_speaking
            segments = self.mic_vad.feed(mono_16k)
            is_speaking = self.mic_vad.is_speaking
            streaming_text = self._feed_streaming_asr(
                self.MIC_STREAM_ID,
                mono_16k,
                was_speaking,
                is_speaking,
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
                    asyncio.run_coroutine_threadsafe(
                        self._process_mic_segment(
                            seg.samples,
                            streaming_text if index == 0 else None,
                        ),
                        self._loop,
                    )
        except Exception as e:
            self.logger.error(f"Mic pipeline frame error: {e}")

    async def _process_segment(
        self,
        samples_16k: np.ndarray,
        streaming_text: Optional[str] = None,
    ):
        """Finish speaker identification and publish a completed utterance."""
        try:
            duration = len(samples_16k) / self.TARGET_SR
            self.logger.info(f"Processing speech segment: {duration:.2f}s")

            # Denoising is retained for speaker identification and one-shot
            # fallback. The primary ASR path has already decoded incrementally.
            if self.denoiser.enabled:
                loop = asyncio.get_running_loop()
                samples_16k = await loop.run_in_executor(
                    None, self._denoise_segment, samples_16k
                )

            # 2. Speaker identification
            speaker_id, speaker_name = self.speaker_tracker.identify(samples_16k)

            # 3. Remote mode needs a complete-buffer request. Local online ASR
            # never decodes the completed segment a second time.
            text = streaming_text
            if not text and not self.asr.supports_streaming:
                text = await self.asr.recognize_segment_async(samples_16k)

            if text and self.on_result:
                self.logger.info(f"[{speaker_name}] ({speaker_id}): {text}")
                await self.on_result(speaker_id, speaker_name, text)

        except Exception as e:
            self.logger.exception(f"Segment processing error: {e}")

    async def _process_mic_segment(
        self,
        samples_16k: np.ndarray,
        streaming_text: Optional[str] = None,
    ):
        """Publish a completed microphone utterance with the fixed owner ID."""
        try:
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

            text = streaming_text
            if not text and not self.asr.supports_streaming:
                text = await self.asr.recognize_segment_async(samples_16k)

            if text and self.on_result:
                self.logger.info(f"[{speaker_name}] ({speaker_id}) [Mic]: {text}")
                await self.on_result(speaker_id, speaker_name, text)

        except Exception as e:
            self.logger.exception(f"Mic segment processing error: {e}")

    def _feed_streaming_asr(
        self,
        stream_id: str,
        samples_16k: np.ndarray,
        was_speaking: bool,
        is_speaking: bool,
    ) -> Optional[str]:
        """Advance one online recognizer stream from VAD state transitions."""
        if not self.asr.supports_streaming:
            return None

        active = self._asr_active[stream_id]
        if not active:
            self._append_asr_preroll(stream_id, samples_16k)

        if not was_speaking and is_speaking:
            if not self.asr.start_stream(stream_id):
                self._clear_asr_preroll(stream_id)
                return None
            self._asr_active[stream_id] = True
            active = True
            preroll = self._take_asr_preroll(stream_id)
            if preroll.size:
                self.asr.accept_stream_audio(stream_id, preroll)
        elif active:
            self.asr.accept_stream_audio(stream_id, samples_16k)

        if active and was_speaking and not is_speaking:
            final_text = self.asr.finish_stream(stream_id)
            self._asr_active[stream_id] = False
            self._clear_asr_preroll(stream_id)
            return final_text

        return None

    def _append_asr_preroll(
        self,
        stream_id: str,
        samples_16k: np.ndarray,
    ) -> None:
        """Keep a short lead-in so VAD activation does not clip first syllables."""
        samples = np.ascontiguousarray(samples_16k, dtype=np.float32).ravel().copy()
        if samples.size == 0:
            return

        buffer = self._asr_preroll[stream_id]
        buffer.append(samples)
        self._asr_preroll_samples[stream_id] += samples.size
        max_samples = int(self.TARGET_SR * self.ASR_PREROLL_SECONDS)

        while self._asr_preroll_samples[stream_id] > max_samples and buffer:
            excess = self._asr_preroll_samples[stream_id] - max_samples
            first = buffer[0]
            if first.size <= excess:
                buffer.popleft()
                self._asr_preroll_samples[stream_id] -= first.size
            else:
                buffer[0] = first[excess:].copy()
                self._asr_preroll_samples[stream_id] -= excess

    def _take_asr_preroll(self, stream_id: str) -> np.ndarray:
        buffer = self._asr_preroll[stream_id]
        if not buffer:
            return np.empty(0, dtype=np.float32)
        samples = np.concatenate(tuple(buffer)).astype(np.float32, copy=False)
        self._clear_asr_preroll(stream_id)
        return np.ascontiguousarray(samples)

    def _clear_asr_preroll(self, stream_id: str) -> None:
        self._asr_preroll[stream_id].clear()
        self._asr_preroll_samples[stream_id] = 0

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
