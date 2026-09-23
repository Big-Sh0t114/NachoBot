"""Typed local multimodal runtime API served on the Core-only 9874 port.

Core selects LOCAL_MULTIMODAL versus REMOTE_API. This service owns concrete
Florence/Sherpa perception models and never receives ordinary platform chat
messages or creates chat turns.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import toml
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .local_runtime import LocalMultimodalRuntime, UnsupportedOperation
from nachobot_multimodal.utils.uvicorn_logging import install_quiet_access_logging


logger = logging.getLogger("multimodal_api")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "perception.toml"
_MAX_TEXT_CHARS = 10_000
_MAX_VIDEO_BYTES = 64 * 1024 * 1024
_MAX_MEDIA_BASE64_CHARS = 4 * ((_MAX_VIDEO_BYTES + 2) // 3)
_MAX_MEDIA_REQUEST_CHARS = _MAX_MEDIA_BASE64_CHARS + 256
_runtime = LocalMultimodalRuntime(config_dir=_CONFIG_PATH.parent)


class PerceptionBody(BaseModel):
    operation: str = Field(min_length=1, max_length=64)
    data: str = Field(min_length=1, max_length=_MAX_MEDIA_REQUEST_CHARS)
    media_format: str = Field(default="", max_length=32)
    mime_type: str = Field(default="", max_length=96)
    prompt: str = Field(default="", max_length=_MAX_TEXT_CHARS)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _load_config() -> dict[str, Any]:
    try:
        return toml.load(str(_CONFIG_PATH))
    except Exception as exc:
        logger.warning("Failed to load perception.toml (%s), using defaults", type(exc).__name__)
        return {}


def _error_response(status: int, message: str) -> JSONResponse:
    # Never echo media payloads, auth material, or backend configuration.
    return JSONResponse(status_code=status, content={"error": {"message": message}})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Load both local perception models before the listener is ready.

    A failed preload is intentionally re-raised.  Uvicorn then leaves 9874
    unavailable, which lets Core select its remote perception provider instead
    of exposing a health listener that would retry model loading per request.
    """

    if not _runtime.perception_enabled:
        logger.info("Local ASR/VLM disabled; 9874 will report not-ready capabilities")
        yield
        return

    try:
        await _runtime.preload()
    except Exception:
        logger.exception("Local ASR/VLM preload failed; refusing 9874 readiness")
        raise
    logger.info("Local ASR/VLM models preloaded before 9874 readiness")
    yield


app = FastAPI(title="NachoBot Local Multimodal Runtime", version="2.0", lifespan=lifespan)


@app.get("/health")
@app.get("/api/health")
async def health() -> dict[str, Any]:
    payload = await _runtime.health()
    payload = dict(payload)
    payload.setdefault("status", "ok" if payload.get("ready") else "degraded")
    return payload


@app.get("/v1/capabilities")
async def capabilities() -> dict[str, Any]:
    return await _runtime.health()


@app.post("/v1/perception", response_model=None)
async def perception(body: PerceptionBody) -> dict[str, Any] | JSONResponse:
    try:
        text = await _runtime.perceive(
            body.operation,
            body.data,
            media_format=body.media_format,
            prompt=body.prompt,
        )
    except ValueError as exc:
        return _error_response(400, str(exc))
    except UnsupportedOperation as exc:
        return _error_response(501, str(exc))
    except Exception:
        logger.exception("Local perception operation failed: %s", body.operation)
        return _error_response(502, "local perception operation failed")
    if not text:
        return _error_response(502, "local perception returned empty text")
    return {
        "operation": body.operation,
        "text": text[:_MAX_TEXT_CHARS],
        "provider": "local",
        "metadata": {},
    }


@app.post("/v1/audio/transcribe", response_model=None)
@app.post("/v1/audio/transcribe.v1", response_model=None)
async def typed_audio_transcribe(body: PerceptionBody) -> dict[str, Any] | JSONResponse:
    body.operation = _runtime.AUDIO
    return await perception(body)


@app.post("/v1/image/describe", response_model=None)
@app.post("/v1/image/describe.v1", response_model=None)
async def typed_image_describe(body: PerceptionBody) -> dict[str, Any] | JSONResponse:
    body.operation = _runtime.IMAGE
    return await perception(body)


@app.post("/v1/video/understand", response_model=None)
@app.post("/v1/video/understand.v1", response_model=None)
async def typed_video_understand(body: PerceptionBody) -> dict[str, Any] | JSONResponse:
    body.operation = _runtime.VIDEO
    return await perception(body)


# OpenAI-compatible compatibility endpoints remain available for explicitly
# configured legacy perception callers.  Core uses the typed endpoints above.
def _extract_image_b64_from_messages(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image_url = part.get("image_url")
            if not isinstance(image_url, dict):
                continue
            url = image_url.get("url", "")
            if url:
                return str(url)
    return ""


@app.post("/v1/chat/completions", response_model=None)
async def vlm_chat_completions(request: Request) -> dict[str, Any] | JSONResponse:
    data = await request.json()
    image_b64 = _extract_image_b64_from_messages(data.get("messages", []))
    if not image_b64:
        return _error_response(400, "No image_url found in messages")
    try:
        caption = await _runtime.perceive(_runtime.IMAGE, image_b64, media_format="png")
    except UnsupportedOperation as exc:
        return _error_response(501, str(exc))
    except Exception:
        logger.exception("Legacy VLM inference failed")
        return _error_response(502, "local VLM inference failed")
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model", "local-image-captioner"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": caption}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/audio/transcriptions", response_model=None)
async def audio_transcriptions(
    file: UploadFile = File(...),  # noqa: B008
    model: str = Form("local-asr"),
) -> dict[str, Any] | JSONResponse:
    del model
    audio_bytes = await file.read(16 * 1024 * 1024 + 1)
    if not audio_bytes:
        return _error_response(400, "Empty audio file")
    if len(audio_bytes) > 16 * 1024 * 1024:
        return _error_response(413, "audio payload exceeds the 16MB bound")
    try:
        text = await _runtime.perceive(
            _runtime.AUDIO,
            base64.b64encode(audio_bytes).decode("ascii"),
        )
    except UnsupportedOperation as exc:
        return _error_response(501, str(exc))
    except Exception:
        logger.exception("Legacy ASR inference failed")
        return _error_response(502, "local ASR inference failed")
    return {"text": text}


if __name__ == "__main__":
    cfg = _load_config()
    host = os.environ.get("HOST") or cfg.get("perception", {}).get("host", "127.0.0.1")
    port = int(os.environ.get("PORT") or cfg.get("perception", {}).get("port", 9874))
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port)
    install_quiet_access_logging()
    uvicorn.Server(config).run()
