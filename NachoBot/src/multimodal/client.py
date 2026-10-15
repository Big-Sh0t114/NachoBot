"""HTTP client for the adapter-owned local multimodal runtime."""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any, AsyncIterator, Mapping, Optional

import httpx

from .contracts import (
    AUDIO_TRANSCRIBE_V1,
    ASR_STREAM_CHANNELS,
    ASR_STREAM_CHUNK_MAX_BYTES,
    ASR_STREAM_SAMPLE_RATE,
    IMAGE_DESCRIBE_V1,
    MAX_AUDIO_BYTES,
    MAX_TEXT_CHARS,
    VIDEO_UNDERSTAND_V1,
    MediaInput,
    PerceptionResult,
    TTSResult,
    TTSStreamChunk,
    TTSStreamError,
    TTSStreamSpec,
    TTS_STREAM_CHUNK_BYTES,
    TTS_STREAM_CODEC,
    TTS_STREAM_MAX_BYTES,
    TTS_STREAM_MAX_DURATION_SECONDS,
    TTS_STREAM_VERSION,
    encode_base64,
    normalize_operation_payload,
)


def _clean_endpoint(value: str | None, default: str) -> str:
    endpoint = str(value or default).strip().rstrip("/")
    return endpoint


class LocalPerceptionError(RuntimeError):
    """Bounded failure category returned by the local execution backend."""

    def __init__(self, reason: str, *, status_code: int = 503):
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


class LocalMultimodalClient:
    """Call typed 9874 endpoints without importing local model code."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        tts_endpoint: str | None = None,
        timeout: float = 30.0,
        client: Any = None,
    ):
        """Create a client for the separately owned local runtimes.

        Perception is served by the eager, perception-only 9874 runtime. TTS
        is served by the public 9880 engine contract, whose ``/api/tts``
        endpoint returns binary audio. Keeping the two base URLs independent
        lets Core choose local perception without coupling it to TTS readiness.
        """

        self.endpoint = _clean_endpoint(
            endpoint
            or os.environ.get("NACHOBOT_MULTIMODAL_ENDPOINT")
            or os.environ.get("NACHOBOT_MULTIMODAL_URL"),
            "http://127.0.0.1:9874",
        )
        self.tts_endpoint = _clean_endpoint(
            tts_endpoint
            or os.environ.get("NACHOBOT_TTS_ENDPOINT")
            or os.environ.get("NACHOBOT_TTS_URL"),
            "http://127.0.0.1:9880",
        )
        self.timeout = max(0.1, min(float(timeout), 300.0))
        self._client = client
        self._owns_client = client is None
        self._closed = False

    def _get_http_client(self) -> Any:
        if self._client is None:
            if self._closed:
                raise LocalPerceptionError("runtime_unavailable")
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._client

    async def aclose(self) -> None:
        """Close the persistent transport when this object created it."""

        if not self._owns_client:
            return
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        return await self._request_json_at(self.endpoint, method, path, **kwargs)

    async def _request_json_at(
        self,
        endpoint: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        try:
            client = self._get_http_client()
            response = await client.request(method, f"{endpoint}{path}", **kwargs)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LocalPerceptionError("timeout") from exc
        except httpx.RequestError as exc:
            raise LocalPerceptionError("runtime_unavailable") from exc
        except httpx.HTTPStatusError as exc:
            reason = "inference_failure"
            try:
                payload = exc.response.json()
                if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping):
                    code = payload["error"].get("code")
                    if code in {
                        "busy",
                        "unsupported",
                        "runtime_unavailable",
                        "inference_failure",
                        "invalid_request",
                        "not_found",
                        "sequence_conflict",
                    }:
                        reason = code
            except (ValueError, TypeError):
                pass
            status = int(exc.response.status_code)
            if reason == "inference_failure":
                reason = {
                    400: "invalid_request",
                    404: "not_found",
                    409: "sequence_conflict",
                    501: "unsupported",
                    503: "runtime_unavailable",
                }.get(status, reason)
            mapped_status = {
                "invalid_request": 400,
                "not_found": 404,
                "sequence_conflict": 409,
                "unsupported": 501,
            }.get(reason, 503)
            raise LocalPerceptionError(reason, status_code=mapped_status) from exc
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("local multimodal runtime returned a non-object response")
        return payload

    async def _request_audio(self, method: str, path: str, **kwargs: Any) -> tuple[bytes, Mapping[str, Any]]:
        """Request bounded binary audio from the public 9880 TTS service."""

        url = f"{self.tts_endpoint}{path}"
        client = self._get_http_client()
        response = await client.request(method, url, **kwargs)
        response.raise_for_status()
        content = bytes(response.content)
        headers = getattr(response, "headers", {})
        if not content:
            raise RuntimeError("local TTS runtime returned empty audio")
        if len(content) > MAX_AUDIO_BYTES:
            raise ValueError(f"TTS audio exceeds the {MAX_AUDIO_BYTES} byte bound")
        return content, headers

    async def health(self) -> Mapping[str, Any]:
        """Read observed health/capabilities; never changes the desired profile."""

        try:
            return await self._request("GET", "/v1/capabilities")
        except Exception:
            # Older local runtimes expose only /api/health.  This fallback is
            # metadata-only and does not reintroduce OpenAI model routing.
            return await self._request("GET", "/api/health")

    async def tts_health(self) -> Mapping[str, Any]:
        """Read public 9880 health and require an actually loaded model."""

        payload = dict(
            await self._request_json_at(self.tts_endpoint, "GET", "/api/health")
        )
        # 9880's public engine health is the readiness boundary.  A legacy
        # A compatibility relay can report backend names while owning no
        # loaded model, so those fields must never make Core ready.
        status = str(payload.get("status") or "").strip().lower()
        status_ready = status in {"ok", "ready"} or payload.get("ready") is True
        model_loaded = payload.get("model_loaded") is True
        if payload.get("ready") is False:
            status_ready = False
        payload["ready"] = bool(status_ready and model_loaded)
        return payload

    async def perceive(self, request: MediaInput) -> PerceptionResult:
        encoded, _ = normalize_operation_payload(request.operation, request.data)
        payload: dict[str, Any] = {
            "operation": request.operation,
            "data": encoded,
            "media_format": request.media_format,
            "mime_type": request.mime_type,
            "prompt": str(request.prompt or "")[:MAX_TEXT_CHARS],
        }
        payload.update({"metadata": dict(request.metadata)})
        response = await self._request("POST", "/v1/perception", json=payload)
        text = str(response.get("text") or "").strip()
        if not text:
            raise RuntimeError("local multimodal runtime returned empty perception text")
        return PerceptionResult(
            operation=request.operation,
            text=text[:MAX_TEXT_CHARS],
            provider="local",
            metadata=dict(response.get("metadata") or {}),
        )

    async def synthesize_tts(
        self,
        text: str,
        *,
        platform: str = "core",
        text_lang: Optional[str] = None,
    ) -> TTSResult:
        text = str(text or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            raise ValueError("TTS text is empty")
        audio_bytes, headers = await self._request_audio(
            "POST",
            "/api/tts",
            json={"text": text, "platform": str(platform or "core")[:64], "text_lang": text_lang},
        )
        content_type = str(headers.get("content-type", "")).lower()
        audio_format = "wav" if "wav" in content_type or not content_type else content_type.split("/", 1)[-1].split(";", 1)[0]
        return TTSResult(
            text=text,
            audio_base64=encode_base64(audio_bytes, max_bytes=MAX_AUDIO_BYTES),
            audio_format=audio_format,
            provider="local",
        )

    @staticmethod
    def _tts_stream_spec(headers: Mapping[str, Any]) -> TTSStreamSpec:
        version = str(headers.get("X-TTS-Stream-Version", "")).strip()
        codec = str(headers.get("X-Audio-Codec", "")).strip()
        if version != TTS_STREAM_VERSION or codec != TTS_STREAM_CODEC:
            raise TTSStreamError("unsupported", status_code=501)

        def header_integer(name: str, *, minimum: int, maximum: int) -> int:
            value = str(headers.get(name, "")).strip()
            if not value.isascii() or not value.isdecimal():
                raise TTSStreamError("invalid_headers")
            parsed = int(value)
            if not minimum <= parsed <= maximum:
                raise TTSStreamError("invalid_headers")
            return parsed

        sample_rate = header_integer("X-Audio-Sample-Rate", minimum=1, maximum=384_000)
        channels = header_integer("X-Audio-Channels", minimum=1, maximum=8)
        sample_width = header_integer("X-Audio-Sample-Width", minimum=2, maximum=2)
        return TTSStreamSpec(
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            codec=codec,
        )

    async def synthesize_tts_stream(
        self,
        text: str,
        *,
        platform: str = "core",
        text_lang: Optional[str] = None,
    ) -> AsyncIterator[TTSStreamChunk]:
        """Stream bounded, frame-aligned PCM chunks from the public 9880 API."""

        text = str(text or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            raise TTSStreamError("invalid_request")
        payload = {
            "text": text,
            "platform": str(platform or "core")[:64],
            "text_lang": text_lang,
        }
        audio_started = False
        try:
            async with asyncio.timeout(TTS_STREAM_MAX_DURATION_SECONDS):
                client = self._get_http_client()
                async with client.stream(
                    "POST",
                    f"{self.tts_endpoint}/api/tts/stream",
                    json=payload,
                    timeout=httpx.Timeout(120.0, connect=3.0),
                ) as response:
                    response.raise_for_status()
                    spec = self._tts_stream_spec(response.headers)
                    frame_bytes = spec.frame_bytes
                    output_chunk_bytes = TTS_STREAM_CHUNK_BYTES - (
                        TTS_STREAM_CHUNK_BYTES % frame_bytes
                    )
                    pending = b""
                    total_bytes = 0
                    # Omitting chunk_size preserves the transport's first
                    # available bytes instead of buffering up to a fixed
                    # block before the first audio chunk can be relayed.
                    async for raw_chunk in response.aiter_bytes():
                        if not raw_chunk:
                            continue
                        total_bytes += len(raw_chunk)
                        if total_bytes > TTS_STREAM_MAX_BYTES:
                            raise TTSStreamError(
                                "stream_too_large",
                                audio_started=audio_started,
                            )
                        combined = pending + raw_chunk
                        complete_bytes = len(combined) - (len(combined) % frame_bytes)
                        pending = combined[complete_bytes:]
                        complete = combined[:complete_bytes]
                        for offset in range(0, len(complete), output_chunk_bytes):
                            pcm = complete[offset : offset + output_chunk_bytes]
                            if pcm:
                                audio_started = True
                                yield TTSStreamChunk(pcm_s16le=pcm, spec=spec)

                    if pending:
                        raise TTSStreamError(
                            "invalid_pcm_alignment",
                            audio_started=audio_started,
                        )
                    if total_bytes == 0:
                        raise TTSStreamError("empty_audio")
        except TTSStreamError as exc:
            if audio_started and not exc.audio_started:
                raise TTSStreamError(
                    exc.code,
                    audio_started=True,
                    status_code=exc.status_code,
                ) from exc
            raise
        except httpx.HTTPStatusError as exc:
            status_code = int(exc.response.status_code)
            if status_code in {404, 405, 501}:
                code = "unsupported"
            elif 400 <= status_code < 500:
                code = "invalid_request"
            else:
                code = "unavailable"
            raise TTSStreamError(
                code,
                audio_started=audio_started,
                status_code=status_code,
            ) from exc
        except LocalPerceptionError as exc:
            raise TTSStreamError(
                exc.reason,
                audio_started=audio_started,
                status_code=exc.status_code,
            ) from exc
        except TimeoutError as exc:
            raise TTSStreamError(
                "midstream_failure" if audio_started else "timeout",
                audio_started=audio_started,
            ) from exc
        except httpx.TimeoutException as exc:
            raise TTSStreamError(
                "midstream_failure" if audio_started else "timeout",
                audio_started=audio_started,
            ) from exc
        except httpx.RequestError as exc:
            raise TTSStreamError(
                "midstream_failure" if audio_started else "unavailable",
                audio_started=audio_started,
            ) from exc

    async def transcribe(self, data: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=AUDIO_TRANSCRIBE_V1, data=data, **kwargs))

    async def start_audio_stream(self, model_identifier: str) -> str:
        """Open a runtime stream pinned to the already selected ASR model."""

        payload = await self._request(
            "POST",
            "/v1/audio/stream/start",
            json={
                "sample_rate": ASR_STREAM_SAMPLE_RATE,
                "channels": ASR_STREAM_CHANNELS,
                "model": str(model_identifier),
            },
        )
        stream_id = payload.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id.strip() or len(stream_id) > 128:
            raise LocalPerceptionError("unsupported", status_code=501)
        return stream_id

    async def append_audio_stream_chunk(
        self,
        stream_id: str,
        seq: int,
        pcm_s16le: bytes,
    ) -> str:
        """Send one bounded PCM chunk and return runtime partial text."""

        if (
            not isinstance(pcm_s16le, (bytes, bytearray))
            or not pcm_s16le
            or len(pcm_s16le) > ASR_STREAM_CHUNK_MAX_BYTES
            or len(pcm_s16le) % 2
        ):
            raise ValueError("PCM chunk must be nonempty, even-length, and at most 64 KB")
        encoded = base64.b64encode(pcm_s16le).decode("ascii")
        payload = await self._request(
            "POST",
            "/v1/audio/stream/chunk",
            json={"stream_id": stream_id, "seq": seq, "pcm_base64": encoded},
        )
        if payload.get("seq") != seq:
            raise LocalPerceptionError("sequence_conflict", status_code=409)
        partial = payload.get("partial_text", "")
        if not isinstance(partial, str):
            raise LocalPerceptionError("inference_failure")
        return partial.strip()[:MAX_TEXT_CHARS]

    async def finish_audio_stream(self, stream_id: str) -> Mapping[str, Any]:
        """Finish one runtime stream and return its confirmed result payload."""

        return await self._request(
            "POST",
            "/v1/audio/stream/finish",
            json={"stream_id": stream_id},
        )

    async def abort_audio_stream(self, stream_id: str) -> None:
        await self._request(
            "POST",
            "/v1/audio/stream/abort",
            json={"stream_id": stream_id},
        )

    async def describe_image(self, data: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=IMAGE_DESCRIBE_V1, data=data, **kwargs))

    async def understand_video(self, data: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=VIDEO_UNDERSTAND_V1, data=data, **kwargs))
