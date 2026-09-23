"""Core HTTP API for perception and non-chat TTS callers.

The router is mounted on the existing Core ``Server`` application under
``/api/multimodal``.  The existing ``Server`` middleware therefore supplies
the same /api bearer-token policy as every other Core control endpoint.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .contracts import (
    AUDIO_TRANSCRIBE_V1,
    IMAGE_DESCRIBE_V1,
    MAX_MEDIA_REQUEST_CHARS,
    MAX_TEXT_CHARS,
    TTS_SYNTHESIZE_V1,
    VIDEO_UNDERSTAND_V1,
)
from .router import CoreMultimodalRouter, get_multimodal_router


class PerceptionBody(BaseModel):
    operation: str = Field(min_length=1, max_length=64)
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
        return await perception(body)

    @router.post("/image/describe/v1")
    @router.post("/image/describe.v1")
    async def describe_image(body: PerceptionBody) -> dict[str, Any]:
        body.operation = IMAGE_DESCRIBE_V1
        return await perception(body)

    @router.post("/video/understand/v1")
    @router.post("/video/understand.v1")
    async def understand_video(body: PerceptionBody) -> dict[str, Any]:
        body.operation = VIDEO_UNDERSTAND_V1
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

    return router


def register_multimodal_api(server: Any, service: CoreMultimodalRouter | None = None) -> APIRouter:
    """Register the facade under /api/multimodal on a Core Server."""

    router = create_multimodal_router(service)
    server.register_router(router, prefix="/api/multimodal")
    return router
