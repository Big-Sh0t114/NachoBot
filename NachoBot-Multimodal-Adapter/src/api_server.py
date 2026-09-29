"""Typed local multimodal runtime API served on the Core-only 9874 port.

Core selects each configured model candidate and invokes this service only
for Florence/Sherpa local execution. This service never receives ordinary
platform chat messages or creates chat turns.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import asynccontextmanager
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import toml
from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, StrictStr

from .local_runtime import (
    AudioStreamConflict,
    AudioStreamInferenceError,
    LocalBusy,
    LocalMultimodalRuntime,
    RuntimeUnavailable,
    UnsupportedOperation,
)
from nachobot_multimodal.utils.uvicorn_logging import install_quiet_access_logging


logger = logging.getLogger("multimodal_api")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "perception.toml"
_MAX_TEXT_CHARS = 10_000
_MAX_VIDEO_BYTES = 64 * 1024 * 1024
_MAX_MEDIA_BASE64_CHARS = 4 * ((_MAX_VIDEO_BYTES + 2) // 3)
_MAX_MEDIA_REQUEST_CHARS = _MAX_MEDIA_BASE64_CHARS + 256
_MAX_AUDIO_STREAM_CHUNK_BASE64_CHARS = 4 * ((64 * 1024 + 2) // 3)
_runtime = LocalMultimodalRuntime(config_dir=_CONFIG_PATH.parent)


class PerceptionBody(BaseModel):
    operation: str = Field(min_length=1, max_length=64)
    data: str = Field(min_length=1, max_length=_MAX_MEDIA_REQUEST_CHARS)
    media_format: str = Field(default="", max_length=32)
    mime_type: str = Field(default="", max_length=96)
    prompt: str = Field(default="", max_length=_MAX_TEXT_CHARS)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AudioStreamStartBody(BaseModel):
    model: StrictStr = Field(min_length=1, max_length=128)
    sample_rate: StrictInt
    channels: StrictInt


class AudioStreamChunkBody(BaseModel):
    stream_id: StrictStr = Field(min_length=1, max_length=64)
    seq: StrictInt = Field(ge=0)
    pcm_base64: StrictStr = Field(min_length=1, max_length=_MAX_AUDIO_STREAM_CHUNK_BASE64_CHARS)


class AudioStreamBody(BaseModel):
    stream_id: StrictStr = Field(min_length=1, max_length=64)


def _load_config() -> dict[str, Any]:
    try:
        return toml.load(str(_CONFIG_PATH))
    except Exception as exc:
        logger.warning("Failed to load perception.toml (%s), using defaults", type(exc).__name__)
        return {}


def _error_response(status: int, message: str, code: str = "inference_failure") -> JSONResponse:
    # Never echo media payloads, auth material, or backend configuration.
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Load both local perception models before the listener is ready.

    A failed preload is intentionally re-raised.  Uvicorn then leaves 9874
    unavailable, which lets Core select its remote perception provider instead
    of exposing a health listener that would retry model loading per request.
    """

    try:
        if not _runtime.perception_enabled:
            logger.info("Local ASR/VLM disabled; 9874 will report not-ready capabilities")
        else:
            await _runtime.preload()
            logger.info("Local ASR/VLM models preloaded before 9874 readiness")
        start_reaper = getattr(_runtime, "start_audio_stream_reaper", None)
        if callable(start_reaper):
            await start_reaper()
        yield
    except Exception:
        logger.exception("Local ASR/VLM preload failed; refusing 9874 readiness")
        raise
    finally:
        close_streams = getattr(_runtime, "close_audio_streams", None)
        if callable(close_streams):
            await close_streams()


app = FastAPI(title="NachoBot Local Multimodal Runtime", version="2.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def stream_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    if request.url.path.startswith("/v1/audio/stream/"):
        return _error_response(400, "invalid audio stream request", "invalid_request")
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": exc.errors()}),
    )


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


def _decode_pcm_base64(value: str) -> bytes:
    if len(value) > _MAX_AUDIO_STREAM_CHUNK_BASE64_CHARS:
        raise ValueError("PCM chunk exceeds 64KB")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("pcm_base64 must be canonical base64") from exc
    if not decoded:
        raise ValueError("PCM chunk is empty")
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("pcm_base64 must be canonical base64")
    if len(decoded) > 64 * 1024:
        raise ValueError("PCM chunk exceeds 64KB")
    if len(decoded) % 2:
        raise ValueError("PCM chunk must contain complete s16le mono frames")
    return decoded


def _audio_stream_error(status: int, message: str, code: str) -> JSONResponse:
    return _error_response(status, message, code)


@app.post("/v1/audio/stream/start", response_model=None)
async def audio_stream_start(body: AudioStreamStartBody) -> dict[str, str] | JSONResponse:
    try:
        stream_id = await _runtime.start_audio_stream(
            model=body.model,
            sample_rate=body.sample_rate,
            channels=body.channels,
        )
    except ValueError as exc:
        return _audio_stream_error(400, str(exc), "invalid_request")
    except LocalBusy:
        return _audio_stream_error(503, "streaming ASR is busy", "busy")
    except RuntimeUnavailable:
        return _audio_stream_error(503, "local streaming ASR is unavailable", "runtime_unavailable")
    except UnsupportedOperation as exc:
        return _audio_stream_error(501, str(exc), "unsupported")
    except AudioStreamInferenceError:
        logger.exception("Local ASR stream start failed")
        return _audio_stream_error(502, "local ASR stream could not start", "inference_failure")
    except Exception:
        logger.exception("Local ASR stream start failed")
        return _audio_stream_error(502, "local ASR stream could not start", "inference_failure")
    return {"stream_id": stream_id}


@app.post("/v1/audio/stream/chunk", response_model=None)
async def audio_stream_chunk(body: AudioStreamChunkBody) -> dict[str, Any] | JSONResponse:
    try:
        pcm_bytes = _decode_pcm_base64(body.pcm_base64)
        partial_text = await _runtime.accept_audio_stream_chunk(
            stream_id=body.stream_id,
            seq=body.seq,
            pcm_bytes=pcm_bytes,
        )
    except ValueError as exc:
        return _audio_stream_error(400, str(exc), "invalid_request")
    except KeyError:
        return _audio_stream_error(404, "audio stream was not found or has expired", "not_found")
    except AudioStreamConflict as exc:
        return _audio_stream_error(409, str(exc), "conflict")
    except AudioStreamInferenceError as exc:
        logger.exception("Local ASR stream chunk failed")
        return _audio_stream_error(502, str(exc), "chunk_failure")
    except RuntimeUnavailable:
        return _audio_stream_error(503, "local streaming ASR is unavailable", "runtime_unavailable")
    except UnsupportedOperation as exc:
        return _audio_stream_error(501, str(exc), "unsupported")
    except Exception:
        logger.exception("Local ASR stream chunk failed")
        return _audio_stream_error(502, "local ASR stream chunk failed", "chunk_failure")
    return {"seq": body.seq, "partial_text": partial_text}


@app.post("/v1/audio/stream/finish", response_model=None)
async def audio_stream_finish(body: AudioStreamBody) -> dict[str, str] | JSONResponse:
    try:
        text = await _runtime.finish_audio_stream(body.stream_id)
    except KeyError:
        return _audio_stream_error(404, "audio stream was not found or has expired", "not_found")
    except AudioStreamConflict as exc:
        return _audio_stream_error(409, str(exc), "conflict")
    except AudioStreamInferenceError as exc:
        logger.exception("Local ASR stream finalization failed")
        return _audio_stream_error(502, str(exc), "finalization_failure")
    except RuntimeUnavailable:
        return _audio_stream_error(503, "local streaming ASR is unavailable", "runtime_unavailable")
    except UnsupportedOperation as exc:
        return _audio_stream_error(501, str(exc), "unsupported")
    except Exception:
        logger.exception("Local ASR stream finalization failed")
        return _audio_stream_error(502, "local ASR stream finalization failed", "finalization_failure")
    return {"text": text}


@app.post("/v1/audio/stream/abort", response_model=None)
async def audio_stream_abort(body: AudioStreamBody) -> dict[str, bool] | JSONResponse:
    try:
        await _runtime.abort_audio_stream(body.stream_id)
    except KeyError:
        return _audio_stream_error(404, "audio stream was not found or has expired", "not_found")
    except AudioStreamConflict as exc:
        return _audio_stream_error(409, str(exc), "conflict")
    except RuntimeUnavailable:
        return _audio_stream_error(503, "local streaming ASR is unavailable", "runtime_unavailable")
    except UnsupportedOperation as exc:
        return _audio_stream_error(501, str(exc), "unsupported")
    except Exception:
        logger.exception("Local ASR stream abort failed")
        return _audio_stream_error(502, "local ASR stream abort failed", "inference_failure")
    return {"aborted": True}


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
        return _error_response(400, str(exc), "invalid_request")
    except LocalBusy:
        return _error_response(503, "local Florence worker is busy", "busy")
    except RuntimeUnavailable:
        return _error_response(503, "local perception runtime unavailable", "runtime_unavailable")
    except UnsupportedOperation as exc:
        return _error_response(501, str(exc), "unsupported")
    except Exception:
        logger.exception("Local perception operation failed: %s", body.operation)
        return _error_response(502, "local perception operation failed", "inference_failure")
    if not text:
        return _error_response(502, "local perception returned empty text", "inference_failure")
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
    except LocalBusy:
        return _error_response(503, "local Florence worker is busy", "busy")
    except RuntimeUnavailable:
        return _error_response(503, "local perception runtime unavailable", "runtime_unavailable")
    except UnsupportedOperation as exc:
        return _error_response(501, str(exc), "unsupported")
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
    except RuntimeUnavailable:
        return _error_response(503, "local perception runtime unavailable", "runtime_unavailable")
    except UnsupportedOperation as exc:
        return _error_response(501, str(exc), "unsupported")
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
