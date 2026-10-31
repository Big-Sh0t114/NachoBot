"""
Voice Activity Detection Module — Silero VAD via sherpa-onnx.

Replaces the original simple RMS-threshold VAD with a neural-network-based
detector that is robust against background noise, music, and keyboard sounds.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from .runtime_compat import preload_onnxruntime

preload_onnxruntime()

try:
    import sherpa_onnx
    _SHERPA_AVAILABLE = True
except (ImportError, OSError):
    _SHERPA_AVAILABLE = False


@dataclass
class SpeechSegment:
    """A detected speech segment with its audio samples."""
    samples: np.ndarray   # float32 mono @ 16kHz
    start_time: float     # seconds from stream start
    duration: float       # seconds


class VADProcessor:
    """Silero VAD wrapper providing speech segment detection."""

    SAMPLE_RATE = 16000
    PCM16_QUANTUM = 1.0 / 32768.0

    def __init__(self, model_path: str, threshold: float = 0.5,
                 min_silence_duration: float = 0.25,
                 min_speech_duration: float = 0.3,
                 logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._vad = None
        self._is_speaking = False
        self._samples_processed = 0

        if not _SHERPA_AVAILABLE:
            self.logger.error("sherpa-onnx not installed. VAD disabled.")
            return

        try:
            vad_config = sherpa_onnx.VadModelConfig()
            vad_config.silero_vad.model = model_path
            vad_config.silero_vad.threshold = threshold
            vad_config.silero_vad.min_silence_duration = min_silence_duration
            vad_config.silero_vad.min_speech_duration = min_speech_duration
            vad_config.sample_rate = self.SAMPLE_RATE

            self._vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=30)
            self.logger.info(f"Silero VAD initialized: threshold={threshold}")
        except Exception as e:
            self.logger.error(f"Failed to initialize Silero VAD: {e}")
            self._vad = None

    @property
    def available(self) -> bool:
        return self._vad is not None

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    def feed(self, samples_16k: np.ndarray) -> List[SpeechSegment]:
        """Feed 16kHz mono float32 samples and return completed segments."""
        if self._vad is None:
            return []

        # Muted QQ capture can contain isolated +/-1 PCM16-unit samples. They
        # are quantization residue, yet the native detector has reported them
        # as speech. Feed silence for a frame wholly within that one-unit floor;
        # leave every frame with a larger signal untouched, including quiet speech.
        has_signal = bool(np.any(np.abs(samples_16k) > self.PCM16_QUANTUM))
        self._vad.accept_waveform(samples_16k if has_signal else np.zeros_like(samples_16k))

        segments: List[SpeechSegment] = []
        completed = False
        while not self._vad.empty():
            seg = self._vad.front
            samples = np.array(seg.samples, dtype=np.float32)
            start_time = seg.start / self.SAMPLE_RATE
            duration = len(samples) / self.SAMPLE_RATE
            self.logger.info(
                "VAD completed segment: samples=%d peak=%.9g finite=%s nonzero=%d",
                samples.size, float(np.max(np.abs(samples))) if samples.size else 0.0,
                bool(np.all(np.isfinite(samples))), int(np.count_nonzero(samples)),
            )
            completed = True
            # A native detection flag is not sufficient evidence of speech:
            # live calls have produced periodic segments at the PCM16 noise floor.
            # Do not send these to ASR or speaker identification.
            if np.any(np.abs(samples) > self.PCM16_QUANTUM):
                segments.append(SpeechSegment(samples=samples, start_time=start_time, duration=duration))
            self._vad.pop()

        if hasattr(self._vad, "is_speech_detected"):
            detected = self._vad.is_speech_detected()
        else:
            detected = not self._vad.empty()

        # Keep a real utterance active through its trailing silence, but never
        # start a new interruption from a frame at the PCM16 noise floor.
        # Completed segments end the previous utterance's evidence, including
        # empty ones. No configurable speech-volume threshold is introduced.
        continuing = self._is_speaking and not completed
        speaking = bool(detected and (continuing or has_signal))
        if speaking and not self._is_speaking:
            self.logger.info(
                "VAD speech start: input_samples=%d peak=%.9g finite=%s",
                samples_16k.size, float(np.max(np.abs(samples_16k))) if samples_16k.size else 0.0,
                bool(np.all(np.isfinite(samples_16k))),
            )
        self._is_speaking = speaking

        self._samples_processed += len(samples_16k)
        return segments

    def reset(self):
        if self._vad is not None:
            # Python exposes reset(), which also discards buffered samples and
            # recurrent state; clearing only queued segments would leak speech
            # across an interruption or a new call.
            self._vad.reset()
        self._is_speaking = False
        self._samples_processed = 0
