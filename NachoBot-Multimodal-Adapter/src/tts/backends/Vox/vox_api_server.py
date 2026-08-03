"""
VoxCPM TTS API Server

独立运行的 FastAPI 服务器，在 VoxCPM 的虚拟环境中启动。
直接加载 VoxCPM 模型并提供 /tts HTTP 接口，供 TTS Adapter 的 Vox 插件调用。

用法:
    cd C:\\Users\\BigSh0t\\VoxCPM-2.0.2
    uv run python C:\\Users\\BigSh0t\\Nacho-with-u\\NachoBot-Multimodal-Adapter\\src\\tts\\backends\\Vox\\vox_api_server.py --port 8808
"""

import os
import sys
import io
import re

import logging
import argparse
import numpy as np
import torch
from typing import Optional, List
from pathlib import Path

torch.set_float32_matmul_precision('high')

# 确保 VoxCPM src 目录在 sys.path 中
VOXCPM_DIR = os.environ.get("VOXCPM_DIR", r"C:\Users\BigSh0t\VoxCPM-2.0.2")
voxcpm_src = os.path.join(VOXCPM_DIR, "src")
if voxcpm_src not in sys.path:
    sys.path.insert(0, voxcpm_src)

from fastapi import FastAPI, Query
from fastapi.responses import Response, StreamingResponse
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("vox_api_server")


AUDIO_FILE_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".opus"}


def _resolve_existing_dir(raw_path: str, label: str) -> Optional[Path]:
    text = str(raw_path or "").strip()
    if not text:
        return None
    path = Path(text).expanduser().resolve(strict=False)
    if path.is_dir():
        return path
    logger.warning(f"{label}不存在或不是目录: {path}")
    return None


def _resolve_optional_audio_path(raw_path: str, label: str) -> Optional[str]:
    text = str(raw_path or "").strip()
    if not text:
        return None
    path = Path(text).expanduser().resolve(strict=False)
    if path.suffix.lower() not in AUDIO_FILE_SUFFIXES:
        logger.warning(f"{label}类型不支持: {path}")
        return None
    if not path.is_file():
        logger.warning(f"{label}不存在: {path}")
        return None
    return str(path)


# ======== 文本切句逻辑（仿 GPT-SoVITS） ========

def _split_by_punctuation(text: str, punctuation_set: str) -> List[str]:
    """按指定标点切分文本，标点保留在前一段末尾"""
    pattern = "([" + re.escape(punctuation_set) + "]+)"
    parts = re.split(pattern, text)
    segments = []
    i = 0
    while i < len(parts):
        seg = parts[i]
        # 如果下一个 part 是标点，拼接到当前段
        if i + 1 < len(parts) and re.fullmatch(pattern, parts[i + 1]):
            seg += parts[i + 1]
            i += 2
        else:
            i += 1
        seg = seg.strip()
        if seg:
            segments.append(seg)
    return segments


def _merge_short_segments(segments: List[str], min_length: int = 10) -> List[str]:
    """将过短的片段合并到相邻片段，避免碎片化"""
    if not segments:
        return segments
    merged = [segments[0]]
    for seg in segments[1:]:
        if len(merged[-1]) < min_length:
            merged[-1] += seg
        else:
            merged.append(seg)
    # 如果最后一段太短，合并到倒数第二段
    if len(merged) > 1 and len(merged[-1]) < min_length:
        merged[-2] += merged[-1]
        merged.pop()
    return merged


def split_text_for_tts(text: str, method: str = "cut3", max_length: int = 80) -> List[str]:
    """文本切句，仿 GPT-SoVITS 的 text_split_method 设计

    切句方法:
      - "cut0": 不切分（原样返回）
      - "cut1": 仅按句号/感叹号/问号切分
      - "cut2": 在 cut1 基础上增加逗号/顿号切分
      - "cut3": 在 cut2 基础上增加分号/冒号/省略号切分（默认）
      - "cut4": 每 N 个字符强制切分（按 max_length）
      - "cut5": 先按 cut3 切分，再对超长段按 max_length 二次切分

    Args:
        text: 要切分的文本
        method: 切句方法标识
        max_length: cut4/cut5 模式下每段最大字符数

    Returns:
        切分后的文本片段列表
    """
    text = text.strip()
    if not text:
        return []

    if method == "cut0":
        return [text]

    # 定义各级标点
    punct_level1 = "。！？!?."  # 句号、感叹号、问号
    punct_level2 = punct_level1 + "，,、"  # 增加逗号、顿号
    punct_level3 = punct_level2 + "；;：:…—"  # 增加分号、冒号、省略号、破折号

    if method == "cut1":
        segments = _split_by_punctuation(text, punct_level1)
    elif method == "cut2":
        segments = _split_by_punctuation(text, punct_level2)
    elif method == "cut3":
        segments = _split_by_punctuation(text, punct_level3)
    elif method == "cut4":
        # 纯按字符数切分
        segments = [text[i:i + max_length] for i in range(0, len(text), max_length)]
    elif method == "cut5":
        # 先按标点切，再对超长段二次切分
        segments = _split_by_punctuation(text, punct_level3)
        final = []
        for seg in segments:
            if len(seg) > max_length:
                final.extend(seg[i:i + max_length] for i in range(0, len(seg), max_length))
            else:
                final.append(seg)
        segments = final
    else:
        logger.warning(f"未知的切句方法 '{method}'，回退到 cut3")
        segments = _split_by_punctuation(text, punct_level3)

    # 合并过短片段
    segments = _merge_short_segments(segments)
    return segments if segments else [text]

app = FastAPI(title="VoxCPM TTS API", version="1.0")

# ======== 全局模型实例 ========
voxcpm_model = None
model_sample_rate = 24000  # VoxCPM 默认采样率


def numpy_to_wav_bytes(sample_rate: int, audio_data: np.ndarray) -> bytes:
    """将 numpy 音频数据转换为 WAV 格式的 bytes（使用标准 wave 模块）"""
    import wave

    if audio_data is None or len(audio_data) == 0:
        raise ValueError("音频数据为空，无法生成 WAV")

    if audio_data.dtype != np.float32:
        audio_data = audio_data.astype(np.float32)

    # 过滤 NaN / Inf
    if np.any(np.isnan(audio_data)) or np.any(np.isinf(audio_data)):
        logger.warning("音频数据包含 NaN/Inf，已替换为 0")
        audio_data = np.nan_to_num(audio_data, nan=0.0, posinf=1.0, neginf=-1.0)

    audio_data = np.clip(audio_data, -1.0, 1.0)
    pcm_data = (audio_data * 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data.tobytes())

    return buf.getvalue()


def numpy_to_pcm_bytes(audio_data: np.ndarray) -> bytes:
    """将 numpy 音频数据转换为 raw PCM 16-bit bytes"""
    if audio_data is None or len(audio_data) == 0:
        return b""

    if audio_data.dtype != np.float32:
        audio_data = audio_data.astype(np.float32)

    # 过滤 NaN / Inf
    if np.any(np.isnan(audio_data)) or np.any(np.isinf(audio_data)):
        audio_data = np.nan_to_num(audio_data, nan=0.0, posinf=1.0, neginf=-1.0)

    audio_data = np.clip(audio_data, -1.0, 1.0)
    pcm_data = (audio_data * 32767).astype(np.int16)
    return pcm_data.tobytes()


def load_model(
    model_dir: str,
    lora_weights_path: str = "",
    enable_denoiser: bool = True,
):
    """加载 VoxCPM 模型"""
    global voxcpm_model, model_sample_rate

    import json
    from voxcpm.core import VoxCPM

    resolved_model_dir = _resolve_existing_dir(model_dir, "VoxCPM模型目录")
    if resolved_model_dir is None:
        raise FileNotFoundError(f"VoxCPM模型目录不存在: {model_dir}")

    logger.info(f"Loading VoxCPM model from: {resolved_model_dir}")

    kwargs = dict(
        voxcpm_model_path=str(resolved_model_dir),
        enable_denoiser=enable_denoiser,
        optimize=True,
    )

    lora_dir = _resolve_existing_dir(lora_weights_path, "LoRA权重目录")
    if lora_dir:
        logger.info(f"Loading LoRA weights from: {lora_dir}")
        kwargs["lora_weights_path"] = str(lora_dir)

        # 从 lora_config.json 读取训练时的 LoRA 配置（rank/alpha 等）
        lora_config_file = lora_dir / "lora_config.json"
        if lora_config_file.is_file():
            with lora_config_file.open("r", encoding="utf-8") as f:
                saved_config = json.load(f)
            lora_cfg_data = saved_config.get("lora_config", {})
            logger.info(f"LoRA config from file: r={lora_cfg_data.get('r')}, alpha={lora_cfg_data.get('alpha')}")

            from voxcpm.model.voxcpm2 import LoRAConfig
            kwargs["lora_config"] = LoRAConfig(**lora_cfg_data)
        else:
            logger.warning(f"lora_config.json not found at {lora_config_file}, using default LoRAConfig")

    voxcpm_model = VoxCPM(**kwargs)
    model_sample_rate = voxcpm_model.tts_model.sample_rate
    logger.info(f"VoxCPM model loaded. Sample rate: {model_sample_rate}")


@app.get("/tts")
async def tts(
    text: str = Query(..., description="要合成的文本"),
    text_lang: str = Query("auto", description="文本语言（兼容参数，VoxCPM自动检测）"),
    control_instruction: str = Query("", description="声音控制描述"),
    reference_wav_path: str = Query("", description="参考音频路径（可控克隆）"),
    prompt_text: str = Query("", description="参考音频文本（极致克隆）"),
    prompt_wav_path: str = Query("", description="提示音频路径（极致克隆，默认同reference_wav_path）"),
    cfg_value: float = Query(2.0, description="CFG引导强度"),
    inference_timesteps: int = Query(10, description="LocDiT迭代步数"),
    denoise: str = Query("true", description="是否降噪"),
    normalize: str = Query("false", description="是否文本规范化"),
    media_type: str = Query("wav", description="音频格式（仅支持wav）"),
    streaming_mode: str = Query("false", description="流式模式（暂不支持）"),
    split_method: str = Query("cut3", description="文本切句方法: cut0(不切)/cut1(句号)/cut2(+逗号)/cut3(+分号冒号)/cut4(按字数)/cut5(标点+字数)"),
    max_split_length: int = Query(80, description="cut4/cut5模式下每段最大字符数"),
    segment_gap_ms: int = Query(100, description="句间静音间隔（毫秒），0为不插入"),
):
    """文本转语音接口

    兼容 GPT-SoVITS 风格的 GET 请求接口。
    支持文本切句以防止长句音色漂移，切句后每段独立生成再拼接。
    返回 WAV 格式的音频数据。
    """
    if voxcpm_model is None:
        return Response(
            content='{"message": "模型未加载"}',
            status_code=503,
            media_type="application/json",
        )

    text = (text or "").strip()
    if not text:
        return Response(
            content='{"message": "文本不能为空"}',
            status_code=400,
            media_type="application/json",
        )

    # 解析布尔值
    do_denoise = denoise.lower() in ("true", "1", "yes")
    do_normalize = normalize.lower() in ("true", "1", "yes")

    # 构建控制文本前缀（用于每一段）
    control = (control_instruction or "").strip()

    # 处理参考音频路径
    ref_wav = _resolve_optional_audio_path(reference_wav_path, "参考音频")

    # 极致克隆: prompt_wav_path + prompt_text
    p_wav = _resolve_optional_audio_path(prompt_wav_path, "提示音频")
    p_text = (prompt_text or "").strip() or None

    # 如果有 prompt_text 但没指定 prompt_wav_path，则使用 reference_wav_path
    if p_text and not p_wav and ref_wav:
        p_wav = ref_wav

    # prompt_wav_path 和 prompt_text 必须同时提供
    if (p_wav is None) != (p_text is None):
        p_wav = None
        p_text = None

    # ====== 文本切句 ======
    segments = split_text_for_tts(text, method=split_method, max_length=max_split_length)
    logger.info(
        f"TTS request: text='{text[:80]}...', split_method={split_method}, "
        f"segments={len(segments)}, ref={ref_wav}, prompt={p_wav is not None}"
    )
    if len(segments) > 1:
        logger.info(f"  切句结果: {[s[:30] + '...' if len(s) > 30 else s for s in segments]}")

    try:
        # 构建公共生成参数
        base_kwargs = dict(
            cfg_value=float(cfg_value),
            inference_timesteps=int(inference_timesteps),
            normalize=do_normalize,
            denoise=do_denoise,
        )
        if ref_wav:
            base_kwargs["reference_wav_path"] = ref_wav
        if p_wav and p_text:
            base_kwargs["prompt_wav_path"] = p_wav
            base_kwargs["prompt_text"] = p_text

        # 句间静音（以采样点数表示）
        gap_samples = int(model_sample_rate * max(0, segment_gap_ms) / 1000)
        silence_gap = np.zeros(gap_samples, dtype=np.float32) if gap_samples > 0 else None

        # 逐段生成并拼接
        audio_parts: list[np.ndarray] = []
        for idx, seg in enumerate(segments):
            seg_text = f"({control}){seg}" if control else seg
            generate_kwargs = {**base_kwargs, "text": seg_text}

            logger.info(f"  生成第 {idx + 1}/{len(segments)} 段: '{seg[:50]}'")
            wav_segment = voxcpm_model.generate(**generate_kwargs)

            if wav_segment is not None and len(wav_segment) > 0:
                audio_parts.append(wav_segment)
                # 在段间插入静音间隔（最后一段不加）
                if silence_gap is not None and idx < len(segments) - 1:
                    audio_parts.append(silence_gap)
            else:
                logger.warning(f"  第 {idx + 1} 段生成结果为空，跳过")

        if not audio_parts:
            return Response(
                content='{"message": "所有段落生成结果均为空"}',
                status_code=500,
                media_type="application/json",
            )

        # 拼接所有音频段
        wav = np.concatenate(audio_parts)
        wav_bytes = numpy_to_wav_bytes(model_sample_rate, wav)

        logger.info(
            f"Generated audio: {len(wav_bytes)} bytes, {len(wav)} samples, "
            f"{len(segments)} segments"
        )
        return Response(content=wav_bytes, media_type="audio/wav")

    except Exception as e:
        logger.error(f"TTS generation error: {e}", exc_info=True)
        return Response(
            content=f'{{"message": "生成失败: {str(e)}"}}',
            status_code=500,
            media_type="application/json",
        )


@app.get("/tts_stream")
async def tts_stream(
    text: str = Query(..., description="要合成的文本"),
    text_lang: str = Query("auto", description="文本语言（兼容参数，VoxCPM自动检测）"),
    control_instruction: str = Query("", description="声音控制描述"),
    reference_wav_path: str = Query("", description="参考音频路径（可控克隆）"),
    prompt_text: str = Query("", description="参考音频文本（极致克隆）"),
    prompt_wav_path: str = Query("", description="提示音频路径（极致克隆，默认同reference_wav_path）"),
    cfg_value: float = Query(2.0, description="CFG引导强度"),
    inference_timesteps: int = Query(10, description="LocDiT迭代步数"),
    denoise: str = Query("true", description="是否降噪"),
    normalize: str = Query("false", description="是否文本规范化"),
    media_type: str = Query("wav", description="音频格式（仅支持wav）"),
    streaming_mode: str = Query("true", description="流式模式（已支持）"),
    split_method: str = Query("cut3", description="文本切句方法: cut0(不切)/cut1(句号)/cut2(+逗号)/cut3(+分号冒号)/cut4(按字数)/cut5(标点+字数)"),
    max_split_length: int = Query(80, description="cut4/cut5模式下每段最大字符数"),
    segment_gap_ms: int = Query(100, description="句间静音间隔（毫秒），0为不插入"),
):
    """流式文本转语音接口"""
    if voxcpm_model is None:
        return Response(
            content='{"message": "模型未加载"}',
            status_code=503,
            media_type="application/json",
        )

    text = (text or "").strip()
    if not text:
        return Response(
            content='{"message": "文本不能为空"}',
            status_code=400,
            media_type="application/json",
        )

    do_denoise = denoise.lower() in ("true", "1", "yes")
    do_normalize = normalize.lower() in ("true", "1", "yes")
    control = (control_instruction or "").strip()

    ref_wav = _resolve_optional_audio_path(reference_wav_path, "参考音频")

    p_wav = _resolve_optional_audio_path(prompt_wav_path, "提示音频")
    p_text = (prompt_text or "").strip() or None
    if p_text and not p_wav and ref_wav:
        p_wav = ref_wav
    if (p_wav is None) != (p_text is None):
        p_wav = None
        p_text = None

    segments = split_text_for_tts(text, method=split_method, max_length=max_split_length)
    logger.info(
        f"TTS stream request: text='{text[:80]}...', split_method={split_method}, "
        f"segments={len(segments)}, ref={ref_wav}, prompt={p_wav is not None}"
    )

    def generate_pcm_stream():
        try:
            base_kwargs = dict(
                cfg_value=float(cfg_value),
                inference_timesteps=int(inference_timesteps),
                normalize=do_normalize,
                denoise=do_denoise,
            )
            if ref_wav:
                base_kwargs["reference_wav_path"] = ref_wav
            if p_wav and p_text:
                base_kwargs["prompt_wav_path"] = p_wav
                base_kwargs["prompt_text"] = p_text

            gap_samples = int(model_sample_rate * max(0, segment_gap_ms) / 1000)
            silence_gap_pcm = None
            if gap_samples > 0:
                silence_gap = np.zeros(gap_samples, dtype=np.float32)
                silence_gap_pcm = numpy_to_pcm_bytes(silence_gap)

            for idx, seg in enumerate(segments):
                seg_text = f"({control}){seg}" if control else seg
                generate_kwargs = {**base_kwargs, "text": seg_text}

                logger.info(f"  流式生成第 {idx + 1}/{len(segments)} 段: '{seg[:50]}'")
                generator = voxcpm_model.generate_streaming(**generate_kwargs)

                for chunk in generator:
                    if chunk is not None and len(chunk) > 0:
                        yield numpy_to_pcm_bytes(chunk)

                if silence_gap_pcm is not None and idx < len(segments) - 1:
                    yield silence_gap_pcm

        except Exception as e:
            logger.error(f"TTS stream generation error: {e}", exc_info=True)

    return StreamingResponse(
        generate_pcm_stream(),
        media_type="application/octet-stream",
        headers={
            "X-Sample-Rate": str(model_sample_rate),
            "X-Sample-Width": "2",
            "X-Channels": "1",
        }
    )


@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "ok",
        "model_loaded": voxcpm_model is not None,
        "sample_rate": model_sample_rate,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VoxCPM TTS API Server")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8808)
    parser.add_argument(
        "--model-dir",
        type=str,
        default=os.path.join(VOXCPM_DIR, "models", "openbmb__VoxCPM2"),
    )
    parser.add_argument("--lora-weights", type=str, default="")
    parser.add_argument("--no-denoiser", action="store_true", help="禁用降噪器")
    args = parser.parse_args()

    # 启动时加载模型
    load_model(
        model_dir=args.model_dir,
        lora_weights_path=args.lora_weights,
        enable_denoiser=not args.no_denoiser,
    )

    uvicorn.run(app, host=args.host, port=args.port)
