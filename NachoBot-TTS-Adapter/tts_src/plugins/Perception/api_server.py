# 文件路径：tts_src/plugins/Perception/api_server.py
"""Perception API Server — independent VLM + ASR service.

Provides OpenAI-compatible endpoints for:
  - POST /v1/chat/completions   (Florence-2 image captioning)
  - POST /v1/audio/transcriptions (FunASR speech recognition)

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
_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "configs" / "perception.toml"


def _load_config() -> dict:
    try:
        return toml.load(str(_CONFIG_PATH))
    except Exception as e:
        logger.warning("Failed to load perception.toml (%s), using defaults", e)
        return {}


@app.on_event("startup")
async def startup_event():
    """预加载 VLM 和 ASR 模型（可通过 DISABLE_VLM_ASR=1 跳过）。"""
    from .florence2_vlm import load_model as load_florence2
    from .funasr_asr import load_model as load_funasr

    print("启动 Perception API 服务中 ...")

    if os.environ.get("DISABLE_VLM_ASR") == "1":
        print("[System] DISABLE_VLM_ASR is set to 1. Skipping VLM and ASR preloading.")
        logger.info("DISABLE_VLM_ASR is set. VLM and ASR preloading skipped.")
        return

    print("[Florence-2] Preloading VLM model ...")
    await asyncio.to_thread(load_florence2)
    print("[Florence-2] VLM model loaded")

    print("[FunASR] Preloading ASR model ...")
    await asyncio.to_thread(load_funasr)
    print("[FunASR] ASR model loaded")


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

    Accepts standard OpenAI Chat Completions request with vision content,
    extracts the base64 image, runs Florence-2 captioning, and returns
    a standard OpenAI Chat Completions response.
    """
    from .florence2_vlm import caption_image_b64

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
        caption = await asyncio.to_thread(caption_image_b64, image_b64)
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


# ==================== FunASR 语音识别接口 ====================


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file: UploadFile = File(...),
    model: str = Form("sensevoice-small"),
):
    """OpenAI-compatible audio transcription endpoint backed by FunASR.

    Accepts standard OpenAI Whisper API format (multipart/form-data with
    'file' and 'model' fields) and returns {"text": "..."} response.
    """
    from .funasr_asr import transcribe

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
        logger.error("[FunASR] Transcription error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": f"FunASR transcription failed: {exc}",
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
