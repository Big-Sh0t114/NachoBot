# 文件路径：src/plugins/GPT_SoVITS/api_server.py
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
import uvicorn
import os
import asyncio
import time
import uuid
import logging
from .tts_model import TTSModel

app = FastAPI(title="GPT-SoVITS Adapter API", version="1.1")
logger = logging.getLogger("api_server")

# ======== 全局实例 ========
tts_model: TTSModel | None = None
gpt_weights: str | None = None
sovits_weights: str | None = None


@app.on_event("startup")
async def startup_event():
    """初始化 FastAPI 服务时加载默认模型配置"""
    global tts_model
    from .florence2_vlm import load_model as load_florence2
    from .funasr_asr import load_model as load_funasr

    print("启动 GPT-SoVITS TTS 服务中 ...")
    try:
        tts_model = TTSModel()
        print("默认配置加载完成")
    except Exception as e:
        logger.error(f"TTS Model initialization failed: {e}")
        print(f"TTS 模型加载失败，TTS 功能将不可用。错误: {e}")

    print("[Florence-2] 正在预加载 VLM 模型 ...")
    await asyncio.to_thread(load_florence2)
    print("[Florence-2] VLM 模型加载完成")

    print("[FunASR] 正在预加载 ASR 模型 ...")
    await asyncio.to_thread(load_funasr)
    print("[FunASR] ASR 模型加载完成")


# ==================== 模型加载接口 ====================
@app.post("/load_model")
async def load_model(request: Request):
    """
    动态加载新的 GPT 和 SoVITS 模型权重
    JSON 示例：
    {
        "gpt_path": "C:/Users/BigSh0t/Nacho-with-u/nachobot_tts_adapter/configs/ncnk1-e15.ckpt",
        "sovits_path": "C:/Users/BigSh0t/Nacho-with-u/nachobot_tts_adapter/configs/ncnk1_e10_s370.pth"
    }
    """
    global tts_model, gpt_weights, sovits_weights

    data = await request.json()
    gpt_path = data.get("gpt_path")
    sovits_path = data.get("sovits_path")

    if not gpt_path or not os.path.exists(gpt_path):
        return {"status": "error", "msg": f"GPT模型文件不存在: {gpt_path}"}
    if not sovits_path or not os.path.exists(sovits_path):
        return {"status": "error", "msg": f"SoVITS模型文件不存在: {sovits_path}"}

    # 动态切换权重
    tts_model.set_gpt_weights(gpt_path)
    tts_model.set_sovits_weights(sovits_path)

    gpt_weights, sovits_weights = gpt_path, sovits_path
    return {
        "status": "ok",
        "msg": "模型权重已加载成功",
        "gpt_model": os.path.basename(gpt_path),
        "sovits_model": os.path.basename(sovits_path),
    }


# ==================== 推理接口 ====================
@app.post("/infer")
async def infer(request: Request):
    """
    文本转语音接口
    JSON 示例：
    {
        "text": "你好，我是GPT-SoVITS测试语音。",
        "platform": "default"
    }
    """
    if tts_model is None:
        return {"status": "error", "msg": "TTS 模型未初始化"}

    data = await request.json()
    text = data.get("text", "").strip()
    platform = data.get("platform", "default")

    if not text:
        return {"status": "error", "msg": "缺少文本输入"}

    try:
        # 调用已有的 TTS 接口（返回音频二进制）
        audio_bytes = await tts_model.tts(text=text, platform=platform)
        output_path = f"output_{platform}.wav"
        with open(output_path, "wb") as f:
            f.write(audio_bytes)

        return {"status": "ok", "msg": "语音生成成功", "audio_file": os.path.abspath(output_path)}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


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
    uvicorn.run(app, host="0.0.0.0", port=9872)
