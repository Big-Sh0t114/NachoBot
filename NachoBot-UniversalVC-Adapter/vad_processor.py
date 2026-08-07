"""
Voice Activity Detection Module — Silero VAD via sherpa-onnx.

Replaces the original simple RMS-threshold VAD with a neural-network-based
detector that is robust against background noise, music, and keyboard sounds.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from multimodal_bridge import ensure_multimodal_import

ensure_multimodal_import()

from nachobot_multimodal.asr.onnxruntime_compat import preload_onnxruntime  # noqa: E402

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

        self._vad.accept_waveform(samples_16k)

        segments: List[SpeechSegment] = []
        while not self._vad.empty():
            seg = self._vad.front
            samples = np.array(seg.samples, dtype=np.float32)
            start_time = seg.start / self.SAMPLE_RATE
            duration = len(samples) / self.SAMPLE_RATE
            segments.append(SpeechSegment(samples=samples, start_time=start_time, duration=duration))
            self._vad.pop()

        if hasattr(self._vad, "is_speech_detected"):
            self._is_speaking = self._vad.is_speech_detected()
        else:
            self._is_speaking = not self._vad.empty()

        self._samples_processed += len(samples_16k)
        return segments

    def reset(self):
        if self._vad is not None:
            self._vad.clear()
        self._is_speaking = False
        self._samples_processed = 0
