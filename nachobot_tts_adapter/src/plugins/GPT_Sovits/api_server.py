# 文件路径：src/plugins/GPT_SoVITS/api_server.py
from fastapi import FastAPI, Request
import uvicorn
import os
import asyncio
from .tts_model import TTSModel

app = FastAPI(title="GPT-SoVITS Adapter API", version="1.0")

# ======== 全局实例 ========
tts_model: TTSModel | None = None
gpt_weights: str | None = None
sovits_weights: str | None = None


@app.on_event("startup")
async def startup_event():
    """初始化 FastAPI 服务时加载默认模型配置"""
    global tts_model
    print("启动 GPT-SoVITS TTS 服务中 ...")
    tts_model = TTSModel()
    print("默认配置加载完成")


# ==================== 模型加载接口 ====================
@app.post("/load_model")
async def load_model(request: Request):
    """
    动态加载新的 GPT 和 SoVITS 模型权重
    JSON 示例：
    {
        "gpt_path": "configs/ncnk1-e15.ckpt",
        "sovits_path": "configs/ncnk1_e10_s370.pth"
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


# ==================== 主启动入口 ====================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9872)
