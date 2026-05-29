"""
Streaming ASR Module — sherpa-onnx OnlineRecognizer.

Provides two modes:
  1. local_streaming: Real-time ASR using sherpa-onnx transducer models.
  2. remote_api: Fallback to HTTP-based ASR API (original behaviour).
"""

import io
import logging
import wave
from typing import Optional

import numpy as np
import aiohttp

try:
    import sherpa_onnx
    _SHERPA_AVAILABLE = True
except ImportError:
    _SHERPA_AVAILABLE = False


class StreamingASR:
    """Streaming ASR engine with local and remote modes."""

    SAMPLE_RATE = 16000

    def __init__(self, mode: str = "local_streaming",
                 tokens_path: str = "", encoder_path: str = "",
                 decoder_path: str = "", joiner_path: str = "",
                 num_threads: int = 2,
                 # Remote API fallback params
                 api_key: str = "", base_url: str = "", model: str = "whisper-1",
                 logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.mode = mode
        self._recognizer = None

        # Remote API config
        self._api_key = api_key
        self._base_url = base_url
        self._api_model = model

        if mode == "local_streaming":
            self._init_local(tokens_path, encoder_path, decoder_path, joiner_path, num_threads)
        elif mode == "remote_api":
            self.logger.info("ASR mode: remote_api (HTTP fallback)")
        else:
            self.logger.error(f"Unknown ASR mode: {mode}")

    def _init_local(self, tokens: str, encoder: str, decoder: str, joiner: str, threads: int):
        if not _SHERPA_AVAILABLE:
            self.logger.error("sherpa-onnx not installed. Falling back to remote_api.")
            self.mode = "remote_api"
            return
        try:
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=tokens,
                encoder=encoder,
                decoder=decoder,
                joiner=joiner,
                num_threads=threads,
                sample_rate=self.SAMPLE_RATE,
                feature_dim=80,
            )
            self.logger.info(f"Local streaming ASR initialized (threads={threads})")
        except Exception as e:
            self.logger.error(f"Failed to init local ASR: {e}. Falling back to remote_api.")
            self.mode = "remote_api"

    def recognize_segment(self, samples_16k: np.ndarray) -> Optional[str]:
        """Recognize a complete speech segment (synchronous).

        Args:
            samples_16k: float32 mono audio at 16kHz.

        Returns:
            Recognized text or None.
        """
        if self.mode == "local_streaming" and self._recognizer is not None:
            return self._recognize_local(samples_16k)
        return None  # remote mode needs async

    def _recognize_local(self, samples_16k: np.ndarray) -> Optional[str]:
        """Run local ASR on a speech segment."""
        try:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(self.SAMPLE_RATE, samples_16k)

            # Feed tail padding to flush
            tail_padding = np.zeros(int(self.SAMPLE_RATE * 0.3), dtype=np.float32)
            stream.accept_waveform(self.SAMPLE_RATE, tail_padding)
            stream.input_finished()

            while self._recognizer.is_ready(stream):
                self._recognizer.decode_stream(stream)

            result = self._recognizer.get_result(stream)
            text = result.strip() if isinstance(result, str) else getattr(result, "text", str(result)).strip()
            if text:
                self.logger.info(f"Local ASR: {text}")
                return text
            return None
        except Exception as e:
            self.logger.error(f"Local ASR error: {e}")
            return None

    async def recognize_segment_async(self, samples_16k: np.ndarray,
                                       sample_rate: int = 16000) -> Optional[str]:
        """Recognize a speech segment (async, supports both modes)."""
        if self.mode == "local_streaming" and self._recognizer is not None:
            # Run local ASR in thread to avoid blocking event loop
            import asyncio
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._recognize_local, samples_16k)
        elif self.mode == "remote_api":
            return await self._recognize_remote(samples_16k, sample_rate)
        return None

    async def _recognize_remote(self, samples_16k: np.ndarray,
                                 sample_rate: int = 16000) -> Optional[str]:
        """Call remote ASR API (OpenAI Whisper-compatible)."""
        if not self._api_key:
            self.logger.warning("No ASR API key configured for remote mode.")
            return None

        try:
            # Convert to WAV
            int16_samples = (np.clip(samples_16k, -1.0, 1.0) * 32767).astype(np.int16)
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(int16_samples.tobytes())
            wav_buffer.seek(0)
            wav_data = wav_buffer.read()

            url = f"{self._base_url}/audio/transcriptions"
            headers = {"Authorization": f"Bearer {self._api_key}"}
            data = aiohttp.FormData()
            data.add_field("file", wav_data, filename="audio.wav", content_type="audio/wav")
            data.add_field("model", self._api_model)

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=data) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        self.logger.error(f"Remote ASR error {resp.status}: {err}")
                        return None
                    result = await resp.json()
                    text = result.get("text", "").strip()
                    if text:
                        text = "".join(ch for ch in text if ch.isprintable() or ch in "\n\r\t")
                        self.logger.info(f"Remote ASR: {text}")
                        return text
                    return None
        except Exception as e:
            self.logger.error(f"Remote ASR request failed: {e}")
            return None
