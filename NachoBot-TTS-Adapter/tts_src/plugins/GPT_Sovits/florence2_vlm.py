"""Florence-2-large VLM module for image captioning.

Lazily loads the Microsoft Florence-2-large model on first call
to avoid slowing down the Control API startup.
"""

import base64
import logging
import threading
import os
from io import BytesIO

logger = logging.getLogger("florence2_vlm")

# ── Global model state (lazy-loaded) ──────────────────────────────────
_model = None
_processor = None
_device = None
_lock = threading.Lock()
_loaded = False


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

        _device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if _device == "cuda" else torch.float32

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

    image = Image.open(BytesIO(image_bytes))
    if getattr(image, "is_animated", False):
        image.seek(0)
    image = image.convert("RGB")
    dtype = torch.float16 if _device == "cuda" else torch.float32

    inputs = _processor(text=task, images=image, return_tensors="pt")

    # Debug logging
    logger.info("[Florence-2] Processor output keys: %s", list(inputs.keys()))

    # Move to device and cast dtype (except input_ids which are Long)
    # Florence-2 inputs typically include: 'pixel_values', 'input_ids', 'attention_mask'
    for k, v in inputs.items():
        if k == "input_ids" or k == "attention_mask":
            inputs[k] = v.to(_device)
        else:
            inputs[k] = v.to(_device, dtype)

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
