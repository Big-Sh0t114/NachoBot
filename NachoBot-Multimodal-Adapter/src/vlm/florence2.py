"""Florence-2-large VLM module for image captioning.

Lazily loads the Transformers-native Florence-2-large model on first call.
Independent of any TTS plugin — reads device config from perception.toml.
"""

import base64
import logging
import threading
import os
from io import BytesIO
from pathlib import Path

import toml

logger = logging.getLogger("florence2_vlm")

FLORENCE_DETAILED_CAPTION_TASK = "<MORE_DETAILED_CAPTION>"
FLORENCE_DETAILED_CAPTION_MAX_NEW_TOKENS = 256
FLORENCE_DETAILED_CAPTION_NUM_BEAMS = 3

# ── Global model state (lazy-loaded) ──────────────────────────────────
_model = None
_processor = None
_device = None
_lock = threading.Lock()
_loaded = False

# ── Config path ────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "perception.toml"


def _read_device() -> str:
    """Read VLM device setting from perception.toml."""
    try:
        cfg = toml.load(str(_CONFIG_PATH))
        return cfg.get("perception", {}).get("device", {}).get("vlm", "cuda:0")
    except Exception as e:
        logger.warning("[Florence-2] Failed to read device from config (%s), defaulting to cuda", e)
        return "cuda"


def load_model():
    """Load the Florence-2-large model into VRAM."""
    global _model, _processor, _device, _loaded

    # Third-party mirrors may not expose the complete Florence community repo.
    # Use the official Hugging Face endpoint by default while allowing an explicit override.
    os.environ["HF_ENDPOINT"] = os.getenv("NACHOBOT_HF_ENDPOINT", "https://huggingface.co")
    # Fail fast when the Hub/mirror is unreachable, then fall back to local cache.
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "3")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "5")

    if _loaded:
        return

    with _lock:
        if _loaded:
            return

        import torch
        from transformers import AutoProcessor, Florence2ForConditionalGeneration

        model_id = "florence-community/Florence-2-large"
        cache_dir = Path(__file__).resolve().parents[2] / "models" / "hf_cache" / "hub"
        cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[Florence-2] Loading model: %s ...", model_id)
        logger.info("[Florence-2] Hugging Face cache: %s", cache_dir)

        config_device = _read_device()

        if "cuda" in config_device and not torch.cuda.is_available():
            logger.warning("[Florence-2] CUDA is not available, falling back to CPU")
            _device = "cpu"
        else:
            _device = config_device

        dtype = torch.float16 if "cuda" in _device else torch.float32

        try:
            logger.info("[Florence-2] Loading model from local cache first")
            _processor = AutoProcessor.from_pretrained(
                model_id,
                cache_dir=str(cache_dir),
                local_files_only=True,
            )
            _model = Florence2ForConditionalGeneration.from_pretrained(
                model_id,
                cache_dir=str(cache_dir),
                dtype=dtype,
                use_safetensors=True,
                local_files_only=True,
            ).to(_device)
            logger.info("[Florence-2] Model loaded from local cache")
        except Exception as exc:
            logger.warning(
                "[Florence-2] Local cache unavailable; trying Hub: %s",
                exc,
            )
            _processor = AutoProcessor.from_pretrained(
                model_id,
                cache_dir=str(cache_dir),
            )
            _model = Florence2ForConditionalGeneration.from_pretrained(
                model_id,
                cache_dir=str(cache_dir),
                dtype=dtype,
                use_safetensors=True,
            ).to(_device)
            logger.info("[Florence-2] Model loaded from Hub")

        _model.eval()

        _loaded = True
        logger.info("[Florence-2] Model loaded on %s (dtype=%s)", _device, dtype)


def caption_image(
    image_bytes: bytes,
    task: str = FLORENCE_DETAILED_CAPTION_TASK,
    text_input: str | None = None,
    max_new_tokens: int = FLORENCE_DETAILED_CAPTION_MAX_NEW_TOKENS,
    num_beams: int = FLORENCE_DETAILED_CAPTION_NUM_BEAMS,
    do_sample: bool = False,
    temperature: float | None = None,
) -> str:
    """Run a caller-selected Florence task for the given image bytes.

    Args:
        image_bytes: Raw image bytes (PNG, JPEG, etc.)
        task: Florence-2 task token such as ``<CAPTION>`` or ``<OCR>``.
        text_input: Optional task input for grounding/referring tasks.
        max_new_tokens: Maximum output tokens selected by the caller.
        num_beams: Beam count selected by the caller.
        do_sample: Whether generation should sample.
        temperature: Sampling temperature when sampling is enabled.

    Returns:
        Generated caption string.
    """
    import torch
    from PIL import Image

    load_model()

    task = str(task or "").strip()
    if not task:
        raise ValueError("Florence task must not be empty")
    if max_new_tokens <= 0:
        raise ValueError("max_new_tokens must be greater than zero")
    if num_beams <= 0:
        raise ValueError("num_beams must be greater than zero")

    raw_image = Image.open(BytesIO(image_bytes))

    # ── 处理动图：提取第一帧为独立静态图 ──────────────────────────
    if getattr(raw_image, "is_animated", False):
        raw_image.seek(0)
        # 在全新的 RGB 画布上渲染第一帧，彻底脱离动图源
        # 不对动图 Image 对象直接调用 convert()，避免 PIL 访问
        # 动画帧序列 / 调色板 / 透明度等元数据时触发 C 层崩溃
        frame = raw_image.copy()          # 复制当前帧的像素
        image = Image.new("RGB", frame.size, (255, 255, 255))
        try:
            image.paste(frame, mask=frame.convert("RGBA").split()[3])
        except Exception:
            # 如果 alpha 提取失败（某些 GIF 无透明通道），直接粘贴
            image.paste(frame.convert("RGB"))
        del frame
    elif raw_image.mode in ("RGBA", "LA") or (
        raw_image.mode == "P" and "transparency" in raw_image.info
    ):
        # 静态图但带透明通道
        rgba = raw_image.convert("RGBA")
        image = Image.new("RGB", rgba.size, (255, 255, 255))
        image.paste(rgba, mask=rgba.split()[3])
    else:
        image = raw_image.convert("RGB")

    # 强制深拷贝并清空元数据，彻底阻断与原文件的内存联系
    image = image.copy()
    image.info.clear()
    raw_image.close()
    dtype = torch.float16 if _device and "cuda" in str(_device) else torch.float32

    processor_prompt = task
    if text_input:
        processor_prompt += str(text_input)
    inputs = _processor(
        text=processor_prompt,
        images=image,
        return_tensors="pt",
    )

    # Debug logging
    logger.info("[Florence-2] Processor output keys: %s", list(inputs.keys()))

    # Move to device and cast dtype (except input_ids which are Long)
    # Florence-2 inputs typically include: 'pixel_values', 'input_ids', 'attention_mask'
    for k, v in inputs.items():
        if v.dtype == torch.float32 or v.dtype == torch.float64:
            inputs[k] = v.to(_device, dtype=dtype)
        else:
            inputs[k] = v.to(_device)

    generate_kwargs = {
        "max_new_tokens": int(max_new_tokens),
        "num_beams": int(num_beams),
        "do_sample": bool(do_sample),
    }
    if do_sample and temperature is not None:
        generate_kwargs["temperature"] = float(temperature)

    with torch.no_grad():
        generated_ids = _model.generate(**inputs, **generate_kwargs)

    generated_text = _processor.batch_decode(generated_ids, skip_special_tokens=False)[0]

    # Post-process: Florence-2 returns task token + result
    parsed = _processor.post_process_generation(
        generated_text,
        task=task,
        image_size=(image.width, image.height),
    )

    # parsed is a dict like {"<MORE_DETAILED_CAPTION>": "A photo of ..."}
    result = parsed.get(task, "")
    if isinstance(result, dict):
        # Some tasks return nested dicts
        result = str(result)

    return result.strip()


def caption_image_b64(
    image_b64: str,
) -> str:
    """Run the service-owned detailed-caption policy on a base64 image.

    HTTP callers intentionally cannot select a shorter Florence task or
    override generation settings through this service-facing entry point.
    """
    # Strip optional data URI prefix like "data:image/png;base64,"
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    image_bytes = base64.b64decode(image_b64)
    return caption_image(
        image_bytes,
        task=FLORENCE_DETAILED_CAPTION_TASK,
        text_input=None,
        max_new_tokens=FLORENCE_DETAILED_CAPTION_MAX_NEW_TOKENS,
        num_beams=FLORENCE_DETAILED_CAPTION_NUM_BEAMS,
        do_sample=False,
        temperature=None,
    )


def is_loaded() -> bool:
    """Check whether the model has been loaded."""
    return _loaded
