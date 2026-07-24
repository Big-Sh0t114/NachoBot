"""Florence-2-large VLM module for image captioning.

Lazily loads the Microsoft Florence-2-large model on first call.
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

    # Set HF Mirror for China users if connection fails
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

    if _loaded:
        return

    with _lock:
        if _loaded:
            return

        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor
        from unittest.mock import patch
        from transformers.dynamic_module_utils import get_imports

        # Patch get_imports to ignore flash_attn which is hard to install on Windows
        # Florence-2 code has fallback for when flash_attn is missing, but
        # transformers' check_imports is too strict.
        def fixed_get_imports(filename: str | os.PathLike) -> list[str]:
            imports = get_imports(filename)
            if "flash_attn" in imports:
                imports.remove("flash_attn")
            return imports

        model_id = "microsoft/Florence-2-large"
        logger.info("[Florence-2] Loading model: %s ...", model_id)

        config_device = _read_device()

        if "cuda" in config_device and not torch.cuda.is_available():
            logger.warning("[Florence-2] CUDA is not available, falling back to CPU")
            _device = "cpu"
        else:
            _device = config_device

        dtype = torch.float16 if "cuda" in _device else torch.float32

        # Apply the patch context explicitly during loading
        with patch("transformers.dynamic_module_utils.get_imports", fixed_get_imports):
            _processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
            _model = AutoModelForCausalLM.from_pretrained(
                model_id,
                torch_dtype=dtype,
                trust_remote_code=True,
            ).to(_device)

        _model.eval()

        _loaded = True
        logger.info("[Florence-2] Model loaded on %s (dtype=%s)", _device, dtype)


def caption_image(image_bytes: bytes, task: str = "<MORE_DETAILED_CAPTION>") -> str:
    """Generate a detailed caption for the given image bytes.

    Args:
        image_bytes: Raw image bytes (PNG, JPEG, etc.)
        task: Florence-2 task prompt. Defaults to detailed captioning.

    Returns:
        Generated caption string.
    """
    import torch
    from PIL import Image

    load_model()

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

    inputs = _processor(text=task, images=image, return_tensors="pt")

    # Debug logging
    logger.info("[Florence-2] Processor output keys: %s", list(inputs.keys()))

    # Move to device and cast dtype (except input_ids which are Long)
    # Florence-2 inputs typically include: 'pixel_values', 'input_ids', 'attention_mask'
    for k, v in inputs.items():
        if v.dtype == torch.float32 or v.dtype == torch.float64:
            inputs[k] = v.to(_device, dtype=dtype)
        else:
            inputs[k] = v.to(_device)

    with torch.no_grad():
        generated_ids = _model.generate(
            **inputs,
            max_new_tokens=1024,
            num_beams=1,
            do_sample=False,
        )

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


def caption_image_b64(image_b64: str, task: str = "<MORE_DETAILED_CAPTION>") -> str:
    """Convenience wrapper: accepts base64-encoded image data."""
    # Strip optional data URI prefix like "data:image/png;base64,"
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    image_bytes = base64.b64decode(image_b64)
    return caption_image(image_bytes, task=task)


def is_loaded() -> bool:
    """Check whether the model has been loaded."""
    return _loaded
