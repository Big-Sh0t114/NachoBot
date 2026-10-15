"""
Real-time Audio Denoising Module — RNNoise (via pyrnnoise).

Processes audio frames at 48kHz through RNNoise for noise suppression.
RNNoise operates on 10ms frames (480 samples @ 48kHz), extremely low latency.
"""

import logging
from typing import Optional

import numpy as np

try:
    from pyrnnoise import RNNoise
    _RNNOISE_AVAILABLE = True
except ImportError:
    _RNNOISE_AVAILABLE = False


class DenoiseProcessor:
    """Frame-level real-time denoiser using RNNoise."""

    SAMPLE_RATE = 48000
    FRAME_SIZE = 480  # 10ms @ 48kHz

    def __init__(self, enabled: bool = True, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.enabled = enabled and _RNNOISE_AVAILABLE

        self._denoiser = None

        if enabled and not _RNNOISE_AVAILABLE:
            self.logger.warning(
                "pyrnnoise is not installed. Denoising disabled. "
                "Install with: pip install pyrnnoise"
            )

        if self.enabled:
            self._initialize()

    def _initialize(self):
        """Load the RNNoise model."""
        try:
            self._denoiser = RNNoise(sample_rate=self.SAMPLE_RATE)
            self.logger.info(
                f"RNNoise denoiser initialized: sr={self.SAMPLE_RATE}Hz, "
                f"frame_size={self.FRAME_SIZE} samples (10ms)"
            )
        except Exception as e:
            self.logger.error(f"Failed to initialize RNNoise: {e}")
            self.enabled = False

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Denoise an audio segment.

        Args:
            audio: float32 mono audio at 48kHz. Can be any length.

        Returns:
            Denoised float32 mono audio at 48kHz (same length).
        """
        if not self.enabled or self._denoiser is None:
            return audio

        try:
            # Ensure 1D input
            audio = np.asarray(audio, dtype=np.float32).ravel()

            # RNNoise expects int16 input via denoise_chunk
            # Convert float32 [-1.0, 1.0] to int16
            int16_audio = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)

            # Process through RNNoise (handles arbitrary length, yields 480-sample frames)
            denoised_frames = []
            for _speech_prob, denoised_frame in self._denoiser.denoise_chunk(int16_audio):
                denoised_frames.append(np.asarray(denoised_frame).ravel())

            if not denoised_frames:
                return audio

            # Concatenate to 1D and convert back to float32
            denoised_int16 = np.concatenate(denoised_frames).ravel()

            # Trim or pad to match original length
            orig_len = len(audio)
            if len(denoised_int16) > orig_len:
                denoised_int16 = denoised_int16[:orig_len]
            elif len(denoised_int16) < orig_len:
                denoised_int16 = np.pad(denoised_int16, (0, orig_len - len(denoised_int16)))

            return (denoised_int16.astype(np.float32) / 32767.0)

        except Exception as e:
            self.logger.error(f"Denoise error: {e}")
            return audio

    @property
    def sample_rate(self) -> int:
        """The sample rate expected by the denoiser (48kHz for RNNoise)."""
        return self.SAMPLE_RATE
