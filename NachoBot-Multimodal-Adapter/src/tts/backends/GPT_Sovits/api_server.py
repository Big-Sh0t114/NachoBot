# 文件路径：src/plugins/GPT_SoVITS/api_server.py
from fastapi import FastAPI, Request
import uvicorn
import logging
from pathlib import Path
import re
from .tts_model import TTSModel

app = FastAPI(title="GPT-SoVITS Adapter API", version="1.1")
logger = logging.getLogger("api_server")
OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
GPT_WEIGHT_SUFFIXES = {".ckpt", ".pth", ".pt", ".bin", ".safetensors"}
SOVITS_WEIGHT_SUFFIXES = {".pth", ".pt", ".ckpt", ".safetensors"}
PLATFORM_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# ======== 全局实例 ========
tts_model: TTSModel | None = None
gpt_weights: str | None = None
sovits_weights: str | None = None


def _log_safe(value: object, max_len: int = 200) -> str:
    text = str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    text = "".join(ch if ch >= " " and ch != "\x7f" else "?" for ch in text)
    if len(text) > max_len:
        return text[:max_len].rstrip() + "...[truncated]"
    return text


def _resolve_weight_file(raw_path: str | None, suffixes: set[str], label: str) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise ValueError(f"{label}模型路径不能为空")
    # codeql[py/path-injection]
    path = Path(text).expanduser().resolve(strict=False)
    if path.suffix.lower() not in suffixes:
        raise ValueError(f"{label}模型文件类型不支持: {path.suffix}")
    if not path.is_file():
        raise FileNotFoundError(f"{label}模型文件不存在: {path}")
    return path


def _safe_output_path(platform: str) -> Path:
    token = PLATFORM_FILENAME_RE.sub("_", str(platform or "default")).strip("._")
    if not token:
        token = "default"
    return OUTPUT_DIR / f"output_{token[:80]}.wav"


@app.on_event("startup")
async def startup_event():
    """初始化 FastAPI 服务时加载默认模型配置"""
    global tts_model

    print("启动 GPT-SoVITS TTS 服务中 ...")
    try:
        tts_model = TTSModel()
        print("默认配置加载完成")
    except Exception:
        logger.exception("TTS Model initialization failed")
        print("TTS 模型加载失败，TTS 功能将不可用。")


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

    try:
        gpt_path = _resolve_weight_file(gpt_path, GPT_WEIGHT_SUFFIXES, "GPT")
        sovits_path = _resolve_weight_file(sovits_path, SOVITS_WEIGHT_SUFFIXES, "SoVITS")
    except (FileNotFoundError, ValueError) as e:
        logger.warning("Model path validation failed: %s", _log_safe(e))
        return {"status": "error", "msg": "模型路径无效或文件不存在"}

    # 动态切换权重
    tts_model.set_gpt_weights(str(gpt_path))
    tts_model.set_sovits_weights(str(sovits_path))

    gpt_weights, sovits_weights = str(gpt_path), str(sovits_path)
    return {
        "status": "ok",
        "msg": "模型权重已加载成功",
        "gpt_model": gpt_path.name,
        "sovits_model": sovits_path.name,
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
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = _safe_output_path(platform)
        # codeql[py/path-injection]
        with output_path.open("wb") as f:
            f.write(audio_bytes)

        return {"status": "ok", "msg": "语音生成成功", "audio_file": str(output_path)}
    except Exception as e:
        logger.error("TTS inference failed: %s", _log_safe(e), exc_info=True)
        return {"status": "error", "msg": "语音生成失败"}


# ==================== 主启动入口 ====================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9872)
