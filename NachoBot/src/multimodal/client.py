"""HTTP client for the adapter-owned local multimodal runtime."""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

import httpx

from .contracts import (
    AUDIO_TRANSCRIBE_V1,
    IMAGE_DESCRIBE_V1,
    MAX_AUDIO_BYTES,
    MAX_TEXT_CHARS,
    VIDEO_UNDERSTAND_V1,
    MediaInput,
    PerceptionResult,
    TTSResult,
    encode_base64,
    normalize_operation_payload,
)


def _clean_endpoint(value: str | None, default: str) -> str:
    endpoint = str(value or default).strip().rstrip("/")
    return endpoint


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

    async def _request(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        return await self._request_json_at(self.endpoint, method, path, **kwargs)

    async def _request_json_at(
        self,
        endpoint: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        if self._client is not None:
            response = await self._client.request(method, f"{endpoint}{path}", **kwargs)
        else:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.request(method, f"{endpoint}{path}", **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("local multimodal runtime returned a non-object response")
        return payload

    async def _request_audio(self, method: str, path: str, **kwargs: Any) -> tuple[bytes, Mapping[str, Any]]:
        """Request bounded binary audio from the public 9880 TTS service."""

        url = f"{self.tts_endpoint}{path}"
        if self._client is not None:
            response = await self._client.request(method, url, **kwargs)
            response.raise_for_status()
            content = bytes(response.content)
            headers = getattr(response, "headers", {})
        else:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                content = bytes(response.content)
                headers = response.headers
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

    async def transcribe(self, data: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=AUDIO_TRANSCRIBE_V1, data=data, **kwargs))

    async def describe_image(self, data: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=IMAGE_DESCRIBE_V1, data=data, **kwargs))

    async def understand_video(self, data: str, **kwargs: Any) -> PerceptionResult:
        return await self.perceive(MediaInput(operation=VIDEO_UNDERSTAND_V1, data=data, **kwargs))
