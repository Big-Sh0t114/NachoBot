# 文件路径：src/api_server.py
"""Perception API Server — independent VLM + ASR service.

Provides OpenAI-compatible endpoints for:
  - POST /v1/chat/completions   (Florence-2 image captioning)
  - POST /v1/audio/transcriptions (shared streaming speech recognition)

Completely independent of any TTS plugin (GPT_Sovits / Vox).
"""
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
import uvicorn
import os
import asyncio
import time
import uuid
import logging
from pathlib import Path

import toml

app = FastAPI(title="Perception API (VLM + ASR)", version="1.0")
logger = logging.getLogger("perception_api")

# ── Config ────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "perception.toml"


def _load_config() -> dict:
    try:
        return toml.load(str(_CONFIG_PATH))
    except Exception as e:
        logger.warning("Failed to load perception.toml (%s), using defaults", e)
        return {}


@app.on_event("startup")
async def startup_event():
    """预加载 VLM 和 ASR 模型（可通过 DISABLE_VLM_ASR=1 跳过）。"""
    from .vlm.florence2 import load_model as load_florence2
    from .asr.streaming import load_model as load_streaming_asr

    print("启动 Perception API 服务中 ...")

    if os.environ.get("DISABLE_VLM_ASR") == "1":
        print("[System] DISABLE_VLM_ASR is set to 1. Skipping VLM and ASR preloading.")
        logger.info("DISABLE_VLM_ASR is set. VLM and ASR preloading skipped.")
        return

    print("[Florence-2] Preloading VLM model ...")
    await asyncio.to_thread(load_florence2)
    print("[Florence-2] VLM model loaded")

    print("[Streaming ASR] Preloading shared ASR model ...")
    await asyncio.to_thread(load_streaming_asr)
    print("[Streaming ASR] Shared ASR model loaded")


# ==================== Florence-2 VLM 接口 ====================


def _extract_image_b64_from_messages(messages: list) -> str:
    """从 OpenAI Chat Completions 消息格式中提取 base64 图片数据。

    支持的格式:
    - content 为列表，包含 {"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}
    """
    for msg in reversed(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url:
                    return url
    return ""


@app.post("/v1/chat/completions")
async def vlm_chat_completions(request: Request):
    """OpenAI-compatible VLM endpoint backed by Florence-2-large.

    The endpoint always uses Florence's detailed-caption task and fixed
    deterministic generation settings. Caller-supplied Florence task and
    generation fields are deliberately ignored so no adapter can downgrade
    the lightweight fallback to a terse one-line caption.
    """
    from .vlm.florence2 import caption_image_b64

    data = await request.json()
    messages = data.get("messages", [])

    image_b64 = _extract_image_b64_from_messages(messages)
    if not image_b64:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "No image_url found in messages",
                    "type": "invalid_request_error",
                }
            },
        )

    try:
        caption = await asyncio.to_thread(
            caption_image_b64,
            image_b64,
        )
    except Exception as exc:
        logger.error("[Florence-2] Inference error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": f"Florence-2 inference failed: {exc}",
                    "type": "server_error",
                }
            },
        )

    # Return standard OpenAI Chat Completions response
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    return {
        "id": resp_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model", "florence-2-large"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": caption,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


# ==================== 流式 ASR 语音识别接口 ====================


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file: UploadFile = File(...),
    model: str = Form("zh-xlarge-int8-2025-06-30"),
):
    """OpenAI-compatible upload endpoint backed by the shared online model.

    Accepts standard OpenAI Whisper API format (multipart/form-data with
    'file' and 'model' fields) and returns {"text": "..."} response.
    """
    from .asr.streaming import transcribe

    audio_bytes = await file.read()
    if not audio_bytes:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Empty audio file",
                    "type": "invalid_request_error",
                }
            },
        )

    try:
        text = await asyncio.to_thread(transcribe, audio_bytes)
    except Exception as exc:
        logger.error("[Streaming ASR] Transcription error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": f"Streaming ASR transcription failed: {exc}",
                    "type": "server_error",
                }
            },
        )

    return {"text": text}


# ==================== 主启动入口 ====================
if __name__ == "__main__":
    cfg = _load_config()
    host = cfg.get("perception", {}).get("host", "127.0.0.1")
    port = cfg.get("perception", {}).get("port", 9874)
    uvicorn.run(app, host=host, port=port)
