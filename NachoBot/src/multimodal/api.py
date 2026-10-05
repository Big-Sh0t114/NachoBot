"""Core HTTP API for perception and non-chat TTS callers.

The router is mounted on the existing Core ``Server`` application under
``/api/multimodal``.  The existing ``Server`` middleware therefore supplies
the same /api bearer-token policy as every other Core control endpoint.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .contracts import (
    AUDIO_TRANSCRIBE_V1,
    IMAGE_DESCRIBE_V1,
    MAX_MEDIA_REQUEST_CHARS,
    MAX_TEXT_CHARS,
    TTS_SYNTHESIZE_V1,
    VIDEO_UNDERSTAND_V1,
    normalize_asr_stream_scope,
)
from .router import AudioStreamError, CoreMultimodalRouter, TTSStreamError, get_multimodal_router


class PerceptionBody(BaseModel):
    operation: str = Field(min_length=1, max_length=64)
    task: str = Field(default="", max_length=16)
    data: str = Field(min_length=1, max_length=MAX_MEDIA_REQUEST_CHARS)
    media_format: str = Field(default="", max_length=32)
    mime_type: str = Field(default="", max_length=96)
    prompt: str = Field(default="", max_length=MAX_TEXT_CHARS)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TTSBody(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    platform: str = Field(default="core", max_length=64)
    text_lang: Optional[str] = Field(default=None, max_length=32)


def _perception_payload(result: Any) -> dict[str, Any]:
    return {
        "operation": result.operation,
        "text": result.text,
        "provider": result.provider,
        "degraded": result.degraded,
        "attempted": list(result.attempted),
        "error": result.error,
        "metadata": dict(result.metadata or {}),
    }


async def _stream_json_object(request: Request, *, max_bytes: int = 90_000) -> dict[str, Any]:
    body_parts = []
    body_size = 0
    async for part in request.stream():
        body_size += len(part)
        if body_size > max_bytes:
            raise HTTPException(status_code=400, detail="invalid_request")
        body_parts.append(part)
    raw = b"".join(body_parts)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid_request") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_request")
    return payload


def _stream_http_error(exc: AudioStreamError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.code)


def create_multimodal_router(service: CoreMultimodalRouter | None = None) -> APIRouter:
    """Create a fresh FastAPI router for one Core facade instance."""

    service = service or get_multimodal_router()
    router = APIRouter(tags=["multimodal"])

    @router.get("/health")
    async def health() -> dict[str, Any]:
        observed = await service.health()
        return {
            "status": "ok" if observed.get("ready", observed.get("status") == "ok") else "degraded",
            "desired_profile": service.desired_profile,
            "observed_local": dict(observed),
            "capabilities": {
                "perception": [AUDIO_TRANSCRIBE_V1, IMAGE_DESCRIBE_V1, VIDEO_UNDERSTAND_V1],
                "tts": service.profile.allows_tts,
            },
        }

    @router.get("/capabilities")
    async def capabilities() -> dict[str, Any]:
        observed = await service.health()
        return {
            "desired_profile": service.desired_profile,
            "observed_local": dict(observed),
            "operations": [AUDIO_TRANSCRIBE_V1, IMAGE_DESCRIBE_V1, VIDEO_UNDERSTAND_V1],
            "tts": service.profile.allows_tts,
        }

    @router.post("/perception")
    async def perception(body: PerceptionBody) -> dict[str, Any]:
        try:
            from .contracts import MediaInput

            result = await service.perceive(
                MediaInput(
                    operation=body.operation,
                    data=body.data,
                    task=body.task,
                    media_format=body.media_format,
                    mime_type=body.mime_type,
                    prompt=body.prompt,
                    metadata=body.metadata,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _perception_payload(result)

    @router.post("/audio/transcribe/v1")
    @router.post("/audio/transcribe.v1")
    async def transcribe(body: PerceptionBody) -> dict[str, Any]:
        body.operation = AUDIO_TRANSCRIBE_V1
        body.task = "voice"
        return await perception(body)

    @router.post("/audio/stream/start")
    async def start_audio_stream(request: Request) -> dict[str, Any]:
        body = await _stream_json_object(request, max_bytes=2_048)
        sample_rate = body.get("sample_rate")
        channels = body.get("channels")
        platform = body.get("platform", "universal_vc")
        scope = normalize_asr_stream_scope(body.get("scope", ""))
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or isinstance(channels, bool)
            or not isinstance(channels, int)
        ):
            raise HTTPException(status_code=400, detail="invalid_audio_format")
        if not isinstance(platform, str) or len(platform) > 64:
            raise HTTPException(status_code=400, detail="invalid_platform")
        if scope is None:
            raise HTTPException(status_code=400, detail="invalid_scope")
        try:
            start_kwargs = {
                "sample_rate": sample_rate,
                "channels": channels,
                "platform": platform,
            }
            if scope:
                start_kwargs["scope"] = scope
            return dict(
                await service.start_audio_stream(**start_kwargs)
            )
        except AudioStreamError as exc:
            raise _stream_http_error(exc) from exc

    @router.post("/audio/stream/chunk")
    async def append_audio_stream_chunk(request: Request) -> dict[str, Any]:
        body = await _stream_json_object(request)
        stream_id = body.get("stream_id")
        seq = body.get("seq")
        pcm_base64 = body.get("pcm_base64")
        if (
            not isinstance(stream_id, str)
            or not stream_id.strip()
            or len(stream_id) > 128
            or isinstance(seq, bool)
            or not isinstance(seq, int)
            or seq < 0
            or not isinstance(pcm_base64, str)
        ):
            raise HTTPException(status_code=400, detail="invalid_request")
        try:
            return dict(
                await service.append_audio_stream_chunk(
                    stream_id=stream_id,
                    seq=seq,
                    pcm_base64=pcm_base64,
                )
            )
        except AudioStreamError as exc:
            raise _stream_http_error(exc) from exc

    @router.post("/audio/stream/finish")
    async def finish_audio_stream(request: Request) -> dict[str, Any]:
        body = await _stream_json_object(request, max_bytes=2_048)
        stream_id = body.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id.strip() or len(stream_id) > 128:
            raise HTTPException(status_code=400, detail="invalid_request")
        try:
            return dict(await service.finish_audio_stream(stream_id))
        except AudioStreamError as exc:
            raise _stream_http_error(exc) from exc

    @router.post("/audio/stream/abort")
    async def abort_audio_stream(request: Request) -> dict[str, Any]:
        body = await _stream_json_object(request, max_bytes=2_048)
        stream_id = body.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id.strip() or len(stream_id) > 128:
            raise HTTPException(status_code=400, detail="invalid_request")
        try:
            return dict(await service.abort_audio_stream(stream_id))
        except AudioStreamError as exc:
            raise _stream_http_error(exc) from exc

    @router.post("/image/describe/v1")
    @router.post("/image/describe.v1")
    async def describe_image(body: PerceptionBody) -> dict[str, Any]:
        body.operation = IMAGE_DESCRIBE_V1
        body.task = "vlm"
        return await perception(body)

    @router.post("/image/emoji/v1")
    @router.post("/image/emoji.v1")
    async def describe_emoji(body: PerceptionBody) -> dict[str, Any]:
        body.operation = IMAGE_DESCRIBE_V1
        body.task = "vlm"
        return await perception(body)

    @router.post("/image/describe/fast")
    async def describe_image_fast(body: PerceptionBody) -> dict[str, Any]:
        body.operation = IMAGE_DESCRIBE_V1
        body.task = "vlm_fast"
        return await perception(body)

    @router.post("/video/understand/v1")
    @router.post("/video/understand.v1")
    async def understand_video(body: PerceptionBody) -> dict[str, Any]:
        body.operation = VIDEO_UNDERSTAND_V1
        body.task = "video"
        return await perception(body)

    @router.post("/tts")
    @router.post("/tts/synthesize/v1")
    @router.post("/tts/synthesize.v1")
    @router.post("/tts/synthesize")
    async def synthesize(body: TTSBody) -> dict[str, Any]:
        result = await service.synthesize_tts(
            body.text,
            platform=body.platform,
            text_lang=body.text_lang,
        )
        return {
            "operation": TTS_SYNTHESIZE_V1,
            "text": result.text,
            "audio_base64": result.audio_base64,
            "audio_format": result.audio_format,
            "provider": result.provider,
            "error": result.error,
            "text_only": not result.audio_base64,
        }

    @router.post("/tts/stream")
    async def synthesize_stream(body: TTSBody) -> StreamingResponse:
        """Relay Core-selected TTS as frame-aligned PCM without buffering it."""

        chunks = service.synthesize_tts_stream(
            body.text,
            platform=body.platform,
            text_lang=body.text_lang,
        )
        try:
            first = await anext(chunks)
        except TTSStreamError as exc:
            await chunks.aclose()
            status = exc.status_code or {
                "invalid_request": 400,
                "unsupported": 501,
                "timeout": 504,
                "invalid_headers": 502,
                "invalid_stream_chunk": 502,
                "empty_audio": 502,
            }.get(exc.code, 503)
            raise HTTPException(status_code=status, detail=exc.code) from exc
        except StopAsyncIteration as exc:
            await chunks.aclose()
            raise HTTPException(status_code=502, detail="empty_tts_stream") from exc

        async def audio():
            try:
                yield first.pcm_s16le
                async for chunk in chunks:
                    yield chunk.pcm_s16le
            finally:
                await chunks.aclose()

        spec = first.spec
        return StreamingResponse(
            audio(),
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "X-TTS-Stream-Version": "1",
                "X-Audio-Sample-Rate": str(spec.sample_rate),
                "X-Audio-Channels": str(spec.channels),
                "X-Audio-Sample-Width": str(spec.sample_width),
                "X-Audio-Codec": spec.codec,
            },
        )

    return router


def register_multimodal_api(server: Any, service: CoreMultimodalRouter | None = None) -> APIRouter:
    """Register the facade under /api/multimodal on a Core Server."""

    router = create_multimodal_router(service)
    server.register_router(router, prefix="/api/multimodal")
    return router
