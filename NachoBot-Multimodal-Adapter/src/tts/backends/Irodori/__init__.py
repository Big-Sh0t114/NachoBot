"""Managed Irodori-TTS adapter client.

Heavy Irodori, Torch, and codec dependencies stay in the supervisor-owned
private child process. Importing this module only loads stdlib config and
client code in the multimodal adapter environment.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path
import wave
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nachobot_multimodal.tts.base import (
    BaseTTSModel,
    MAX_TTS_AUDIO_BYTES,
    MAX_TTS_AUDIO_SECONDS,
    PCMChunk,
    collect_pcm_stream_to_wav,
)
from nachobot_multimodal.utils.text_cleaner import clean_text_for_tts

from .config import MAX_TEXT_LENGTH, IrodoriConfigError, load_irodori_config


class IrodoriBackendError(RuntimeError):
    """The managed Irodori inference endpoint returned an invalid response."""


async def _drain_worker(task: asyncio.Task[Any]) -> Any:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _read_wav_pcm(payload: bytes) -> tuple[bytes, int, int]:
    if not payload or len(payload) > MAX_TTS_AUDIO_BYTES:
        raise IrodoriBackendError("Irodori returned empty or oversized audio")
    try:
        with wave.open(BytesIO(payload), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frames = wav_file.getnframes()
            if wav_file.getcomptype() != "NONE" or sample_width != 2 or channels not in {1, 2}:
                raise IrodoriBackendError("Irodori must return uncompressed PCM16 WAV audio")
            if sample_rate <= 0 or frames <= 0:
                raise IrodoriBackendError("Irodori returned invalid WAV metadata")
            pcm = wav_file.readframes(frames)
    except IrodoriBackendError:
        raise
    except (wave.Error, EOFError, OSError):
        raise IrodoriBackendError("Irodori returned an invalid WAV response") from None
    expected = frames * channels * sample_width
    if len(pcm) != expected or not pcm:
        raise IrodoriBackendError("Irodori returned truncated WAV audio")
    if len(pcm) > MAX_TTS_AUDIO_BYTES or frames / sample_rate > MAX_TTS_AUDIO_SECONDS:
        raise IrodoriBackendError("Irodori audio exceeded the configured output budget")
    return pcm, sample_rate, channels


class TTSModel(BaseTTSModel):
    """Buffered PCM16 client to the supervisor-owned Irodori raw engine."""

    native_streaming = False
    stream_capability = "buffered_pcm"

    def __init__(
        self,
        config_path: str | Path | None = None,
        engine_host: str | None = None,
        engine_port: int | None = None,
    ) -> None:
        self.config_path = Path(config_path) if config_path else (
            Path(__file__).resolve().parents[4] / "configs" / "irodori.toml"
        )
        self.config = self.load_config()
        self.host = engine_host or self.config["tts"]["host"]
        self.port = int(engine_port if engine_port is not None else self.config["tts"]["port"])
        if self.host not in {"127.0.0.1", "localhost"} or not 1 <= self.port <= 65535 or self.port == 9880:
            raise IrodoriConfigError("Irodori engine endpoint must use a private loopback address and port")
        self.base_url = f"http://{self.host}:{self.port}"
        self.timeout_seconds = float(self.config["tts"]["timeout_seconds"])

    def load_config(self) -> dict[str, Any]:
        return load_irodori_config(self.config_path)

    @property
    def configuration_ready(self) -> bool:
        try:
            load_irodori_config(self.config_path)
            return True
        except Exception:
            return False

    def _request_sync(self, text: str) -> bytes:
        body = json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}/tts",
            data=body,
            method="POST",
            headers={
                "Accept": "audio/wav",
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(MAX_TTS_AUDIO_BYTES + 1)
                if len(payload) > MAX_TTS_AUDIO_BYTES:
                    raise IrodoriBackendError("Irodori audio exceeded the byte limit")
                if response.headers.get_content_type() != "audio/wav":
                    raise IrodoriBackendError("Irodori returned an unexpected content type")
        except IrodoriBackendError:
            raise
        except HTTPError:
            raise IrodoriBackendError("Irodori synthesis request failed") from None
        except (URLError, TimeoutError, OSError):
            raise IrodoriBackendError("Irodori inference endpoint is unavailable") from None
        _read_wav_pcm(payload)
        return payload

    async def _request_wav(self, text: str) -> bytes:
        task = asyncio.create_task(asyncio.to_thread(self._request_sync, text))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _drain_worker(task)
            raise

    async def startup_warmup(self) -> None:
        """Check lightweight raw health before the public runtime reports ready."""

        task = asyncio.create_task(asyncio.to_thread(self._health_sync))
        try:
            loaded = await asyncio.shield(task)
        except asyncio.CancelledError:
            await _drain_worker(task)
            raise
        if not loaded:
            raise RuntimeError("managed Irodori model is not ready")

    def _health_sync(self) -> bool:
        request = Request(f"{self.base_url}/health", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=min(self.timeout_seconds, 5.0)) as response:
                raw = response.read(64 * 1024)
            payload = json.loads(raw.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and payload.get("model_loaded") is True

    async def tts(self, text: str, **kwargs: Any) -> bytes:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
            raise ValueError("Irodori text must contain 1 to 10000 characters")
        cleaned = clean_text_for_tts(text)
        if not cleaned:
            raise ValueError("清洗后的 TTS 文本为空")
        stream = self.tts_pcm_stream(cleaned, **kwargs)
        return await collect_pcm_stream_to_wav(stream)

    async def tts_stream(self, text: str, **kwargs: Any):
        """Compatibility byte stream; synthesis completes before yielding."""

        yield await self.tts(text, **kwargs)

    async def tts_pcm_stream(self, text: str, **kwargs: Any):
        """Buffer one Irodori result, then expose standard typed PCM chunks."""

        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
            raise ValueError("Irodori text must contain 1 to 10000 characters")
        cleaned = clean_text_for_tts(text)
        if not cleaned:
            raise ValueError("清洗后的 TTS 文本为空")
        payload = await self._request_wav(cleaned)
        pcm, sample_rate, channels = _read_wav_pcm(payload)
        frame_width = channels * 2
        chunk_size = max(frame_width, (64 * 1024 // frame_width) * frame_width)
        for offset in range(0, len(pcm), chunk_size):
            yield PCMChunk(
                data=pcm[offset : offset + chunk_size],
                sample_rate=sample_rate,
                channels=channels,
                sample_width=2,
                codec="pcm_s16le",
            )


__all__ = ["IrodoriBackendError", "TTSModel"]
