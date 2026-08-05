"""Shared incremental speech recognition for NachoBot adapters.

The engine owns the sherpa-onnx online recognizer and the Chinese xlarge INT8
model. UniversalVC feeds live PCM chunks directly, while the Perception API
uses the same engine for uploaded audio compatibility.
"""

import asyncio
from dataclasses import dataclass
from functools import lru_cache
import io
import logging
import os
from pathlib import Path
import threading
from typing import Dict, Optional
import uuid
import wave

import aiohttp
import numpy as np
from scipy.signal import resample_poly
import toml
from static_ffmpeg import run

from .model_manager import ASRModelManager, DEFAULT_MODELS_DIR
from .onnxruntime_compat import preload_onnxruntime


preload_onnxruntime()

try:
    import sherpa_onnx

    _SHERPA_AVAILABLE = True
    _SHERPA_IMPORT_ERROR: Optional[Exception] = None
except (ImportError, OSError) as exc:
    _SHERPA_AVAILABLE = False
    _SHERPA_IMPORT_ERROR = exc


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "perception.toml"


@lru_cache(maxsize=1)
def _configure_pydub_binaries() -> str:
    """配置 pydub 使用项目共享目录中的 FFmpeg 与 FFprobe。"""
    configured_dir = os.environ.get("NACHOBOT_FFMPEG_DIR", "").strip()
    shared_root = (
        Path(configured_dir).expanduser()
        if configured_dir
        else Path(__file__).resolve().parents[3] / ".runtime" / "ffmpeg"
    )
    platform_dir = shared_root.resolve() / run.get_platform_key()
    platform_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_path, ffprobe_path = run.get_or_fetch_platform_executables_else_raise(
        download_dir=str(platform_dir)
    )

    managed_dirs = {
        str(Path(ffmpeg_path).resolve().parent),
        str(Path(ffprobe_path).resolve().parent),
    }
    current_path = os.environ.get("PATH", "")
    current_dirs = set(current_path.split(os.pathsep)) if current_path else set()
    missing_dirs = [directory for directory in managed_dirs if directory not in current_dirs]
    if missing_dirs:
        os.environ["PATH"] = os.pathsep.join([*missing_dirs, current_path])

    return ffmpeg_path


@dataclass(frozen=True)
class ASRSettings:
    mode: str = "local_streaming"
    provider: str = "cpu"
    device: int = 0
    num_threads: int = 4
    models_dir: Path = DEFAULT_MODELS_DIR
    auto_download: bool = True


def load_asr_settings(config_path: str | Path | None = None) -> ASRSettings:
    """Load the single shared ASR configuration from perception.toml."""
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    section: dict = {}
    try:
        section = toml.load(str(path)).get("asr", {})
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Failed to load ASR config %s; using defaults: %s",
            path,
            exc,
        )

    models_dir = Path(section.get("models_dir", "models"))
    if not models_dir.is_absolute():
        models_dir = PROJECT_ROOT / models_dir

    return ASRSettings(
        mode=str(section.get("mode", "local_streaming")),
        provider=str(section.get("provider", "cpu")),
        device=int(section.get("device", 0)),
        num_threads=max(1, int(section.get("num_threads", 4))),
        models_dir=models_dir.resolve(),
        auto_download=bool(section.get("auto_download", True)),
    )


class StreamingASR:
    """Thread-safe sherpa-onnx streaming recognizer shared by all adapters."""

    SAMPLE_RATE = 16000
    TAIL_PADDING_SECONDS = 0.3
    FILE_CHUNK_SECONDS = 0.2

    def __init__(
        self,
        mode: Optional[str] = None,
        tokens_path: str = "",
        encoder_path: str = "",
        decoder_path: str = "",
        joiner_path: str = "",
        num_threads: Optional[int] = None,
        provider: Optional[str] = None,
        device: Optional[int] = None,
        models_dir: str | Path | None = None,
        auto_download: Optional[bool] = None,
        config_path: str | Path | None = None,
        api_key: str = "",
        base_url: str = "",
        model: str = "whisper-1",
        logger: Optional[logging.Logger] = None,
    ):
        settings = load_asr_settings(config_path)
        self.logger = logger or logging.getLogger(__name__)
        self.mode = (mode or settings.mode).strip().lower()
        self.provider = (provider or settings.provider).strip().lower()
        self.device = settings.device if device is None else int(device)
        self.num_threads = (
            settings.num_threads
            if num_threads is None
            else max(1, int(num_threads))
        )
        self.models_dir = (
            settings.models_dir if models_dir is None else Path(models_dir)
        )
        self.auto_download = (
            settings.auto_download if auto_download is None else auto_download
        )

        self._recognizer = None
        self._streams: Dict[str, object] = {}
        self._partial_text: Dict[str, str] = {}
        self._lock = threading.RLock()

        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._api_model = model

        if self.mode == "local_streaming":
            paths = {
                "tokens": tokens_path,
                "encoder": encoder_path,
                "decoder": decoder_path,
                "joiner": joiner_path,
            }
            if not all(paths.values()):
                manager = ASRModelManager(self.models_dir, self.logger)
                if self.auto_download and not manager.ensure_model():
                    self.logger.error(
                        "Shared ASR model is unavailable; falling back to remote API"
                    )
                    self.mode = "remote_api"
                    return
                defaults = manager.get_model_paths()
                for name, value in defaults.items():
                    paths[name] = paths[name] or value

            self._init_local(**paths)
        elif self.mode == "remote_api":
            self.logger.info("ASR mode: remote_api")
        else:
            self.logger.error("Unknown ASR mode: %s", self.mode)

    @property
    def supports_streaming(self) -> bool:
        return self.mode == "local_streaming" and self._recognizer is not None

    def _init_local(
        self,
        tokens: str,
        encoder: str,
        decoder: str,
        joiner: str,
    ) -> None:
        if not _SHERPA_AVAILABLE:
            self.logger.error(
                "sherpa-onnx is unavailable (%s); falling back to remote API",
                _SHERPA_IMPORT_ERROR,
            )
            self.mode = "remote_api"
            return

        model_paths = {
            "tokens": tokens,
            "encoder": encoder,
            "decoder": decoder,
            "joiner": joiner,
        }
        missing = [
            name
            for name, path in model_paths.items()
            if not Path(path).is_file()
        ]
        if missing:
            self.logger.error(
                "Shared ASR model files are missing (%s); falling back to remote API",
                ", ".join(missing),
            )
            self.mode = "remote_api"
            return

        if self.provider not in {"cpu", "cuda", "tensorrt"}:
            self.logger.error(
                "Unsupported sherpa-onnx provider %r; falling back to remote API",
                self.provider,
            )
            self.mode = "remote_api"
            return

        try:
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=tokens,
                encoder=encoder,
                decoder=decoder,
                joiner=joiner,
                num_threads=self.num_threads,
                sample_rate=self.SAMPLE_RATE,
                feature_dim=80,
                decoding_method="greedy_search",
                provider=self.provider,
                device=self.device,
            )
            version = getattr(sherpa_onnx, "__version__", "unknown")
            self.logger.info(
                "Shared streaming ASR initialized: "
                "sherpa-onnx=%s, provider=%s, device=%s, threads=%s",
                version,
                self.provider,
                self.device,
                self.num_threads,
            )
        except Exception as exc:
            self.logger.error(
                "Failed to initialize shared ASR: %s; falling back to remote API",
                exc,
            )
            self.mode = "remote_api"

    @staticmethod
    def _result_text(result: object) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result.strip()
        return getattr(result, "text", str(result)).strip()

    def start_stream(self, stream_id: str) -> bool:
        if not self.supports_streaming:
            return False

        with self._lock:
            if stream_id in self._streams:
                self.logger.warning("Replacing unfinished ASR stream: %s", stream_id)
            self._streams[stream_id] = self._recognizer.create_stream()
            self._partial_text.pop(stream_id, None)
        return True

    def accept_stream_audio(
        self,
        stream_id: str,
        samples_16k: np.ndarray,
    ) -> Optional[str]:
        """Decode one float32 mono 16 kHz chunk and return the latest partial."""
        if not self.supports_streaming:
            return None

        samples = np.ascontiguousarray(samples_16k, dtype=np.float32).ravel()
        if samples.size == 0:
            return self._partial_text.get(stream_id)

        try:
            with self._lock:
                stream = self._streams.get(stream_id)
                if stream is None:
                    stream = self._recognizer.create_stream()
                    self._streams[stream_id] = stream

                stream.accept_waveform(self.SAMPLE_RATE, samples)
                while self._recognizer.is_ready(stream):
                    self._recognizer.decode_stream(stream)

                text = self._result_text(self._recognizer.get_result(stream))
                previous = self._partial_text.get(stream_id)
                if text and text != previous:
                    self._partial_text[stream_id] = text
                    self.logger.debug("ASR partial [%s]: %s", stream_id, text)
                return text or previous
        except Exception as exc:
            self.logger.error("Streaming ASR chunk error [%s]: %s", stream_id, exc)
            self.abort_stream(stream_id)
            return None

    def finish_stream(self, stream_id: str) -> Optional[str]:
        """Flush one stream and return its final text."""
        if not self.supports_streaming:
            return None

        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                return None
            try:
                tail_padding = np.zeros(
                    int(self.SAMPLE_RATE * self.TAIL_PADDING_SECONDS),
                    dtype=np.float32,
                )
                stream.accept_waveform(self.SAMPLE_RATE, tail_padding)
                stream.input_finished()
                while self._recognizer.is_ready(stream):
                    self._recognizer.decode_stream(stream)

                text = self._result_text(self._recognizer.get_result(stream))
                if text:
                    self.logger.info("Streaming ASR [%s]: %s", stream_id, text)
                    return text
                return self._partial_text.get(stream_id)
            except Exception as exc:
                self.logger.error(
                    "Streaming ASR finalization error [%s]: %s",
                    stream_id,
                    exc,
                )
                return self._partial_text.get(stream_id)
            finally:
                self._streams.pop(stream_id, None)
                self._partial_text.pop(stream_id, None)

    def abort_stream(self, stream_id: str) -> None:
        with self._lock:
            self._streams.pop(stream_id, None)
            self._partial_text.pop(stream_id, None)

    def recognize_segment(self, samples_16k: np.ndarray) -> Optional[str]:
        """Use the online recognizer for a complete-buffer compatibility call."""
        if not self.supports_streaming:
            return None

        stream_id = f"buffer-{uuid.uuid4().hex}"
        if not self.start_stream(stream_id):
            return None

        samples = np.ascontiguousarray(samples_16k, dtype=np.float32).ravel()
        chunk_size = int(self.SAMPLE_RATE * self.FILE_CHUNK_SECONDS)
        for offset in range(0, samples.size, chunk_size):
            self.accept_stream_audio(
                stream_id,
                samples[offset : offset + chunk_size],
            )
        return self.finish_stream(stream_id)

    async def recognize_segment_async(
        self,
        samples_16k: np.ndarray,
        sample_rate: int = 16000,
    ) -> Optional[str]:
        if sample_rate != self.SAMPLE_RATE:
            samples_16k = resample_poly(
                samples_16k,
                self.SAMPLE_RATE,
                sample_rate,
            ).astype(np.float32)

        if self.supports_streaming:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                self.recognize_segment,
                samples_16k,
            )
        if self.mode == "remote_api":
            return await self._recognize_remote(samples_16k, self.SAMPLE_RATE)
        return None

    async def _recognize_remote(
        self,
        samples_16k: np.ndarray,
        sample_rate: int,
    ) -> Optional[str]:
        if not self._api_key:
            self.logger.warning("No ASR API key configured for remote mode")
            return None

        try:
            int16_samples = (
                np.clip(samples_16k, -1.0, 1.0) * 32767
            ).astype(np.int16)
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(int16_samples.tobytes())

            url = f"{self._base_url}/audio/transcriptions"
            headers = {"Authorization": f"Bearer {self._api_key}"}
            data = aiohttp.FormData()
            data.add_field(
                "file",
                wav_buffer.getvalue(),
                filename="audio.wav",
                content_type="audio/wav",
            )
            data.add_field("model", self._api_model)

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=data) as response:
                    if response.status != 200:
                        error = await response.text()
                        self.logger.error(
                            "Remote ASR error %s: %s",
                            response.status,
                            error,
                        )
                        return None
                    result = await response.json()
                    text = result.get("text", "").strip()
                    return text or None
        except Exception as exc:
            self.logger.error("Remote ASR request failed: %s", exc)
            return None


def decode_audio_bytes(audio_bytes: bytes) -> np.ndarray:
    """Decode common audio containers to float32 mono 16 kHz samples."""
    if not audio_bytes:
        return np.empty(0, dtype=np.float32)

    try:
        import soundfile as sf

        samples, sample_rate = sf.read(
            io.BytesIO(audio_bytes),
            dtype="float32",
            always_2d=True,
        )
        mono = samples.mean(axis=1)
    except Exception:
        from pydub import AudioSegment

        AudioSegment.converter = _configure_pydub_binaries()
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
        audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
        mono = (
            np.asarray(audio.get_array_of_samples(), dtype=np.float32)
            / 32768.0
        )
        sample_rate = 16000

    if sample_rate != StreamingASR.SAMPLE_RATE:
        mono = resample_poly(
            mono,
            StreamingASR.SAMPLE_RATE,
            sample_rate,
        ).astype(np.float32)
    return np.ascontiguousarray(mono, dtype=np.float32)


_DEFAULT_ASR: Optional[StreamingASR] = None
_DEFAULT_ASR_LOCK = threading.Lock()


def load_model() -> StreamingASR:
    """Create and cache the Perception API's shared streaming recognizer."""
    global _DEFAULT_ASR
    if _DEFAULT_ASR is None:
        with _DEFAULT_ASR_LOCK:
            if _DEFAULT_ASR is None:
                _DEFAULT_ASR = StreamingASR()
    if not _DEFAULT_ASR.supports_streaming:
        raise RuntimeError("Shared local streaming ASR failed to initialize")
    return _DEFAULT_ASR


def transcribe(audio_bytes: bytes) -> str:
    """OpenAI upload-endpoint compatibility using the shared online model."""
    samples = decode_audio_bytes(audio_bytes)
    if samples.size == 0:
        return ""
    return load_model().recognize_segment(samples) or ""


def is_loaded() -> bool:
    return _DEFAULT_ASR is not None and _DEFAULT_ASR.supports_streaming
