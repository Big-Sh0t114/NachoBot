"""FunASR SenseVoiceSmall ASR module for speech-to-text.

Pre-loads the FunAudioLLM/SenseVoiceSmall model at startup
to provide low-latency local speech recognition.
Independent of any TTS plugin — reads device config from perception.toml.
"""

import logging
import os
import threading
from pathlib import Path

import toml

logger = logging.getLogger("funasr_asr")

# ── Global model state ────────────────────────────────────────────────
_model = None
_lock = threading.Lock()
_loaded = False

# ── Config path ────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "configs" / "perception.toml"


def _read_device() -> str:
    """Read ASR device setting from perception.toml."""
    try:
        cfg = toml.load(str(_CONFIG_PATH))
        return cfg.get("perception", {}).get("device", {}).get("asr", "cuda:0")
    except Exception as e:
        logger.warning("[FunASR] Failed to read device from config (%s), defaulting to cuda:0", e)
        return "cuda:0"


def load_model():
    """Load the SenseVoiceSmall ASR model."""
    global _model, _loaded

    # Set HF Mirror for China users
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

    # Add ffmpeg to PATH if available
    ffmpeg_dir = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "NachoBot",
        "plugins",
        "bilibili_video_sender_plugin",
        "ffmpeg",
        "bin",
    )
    if os.path.isdir(ffmpeg_dir) and ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] += os.pathsep + os.path.abspath(ffmpeg_dir)

    if _loaded:
        return

    with _lock:
        if _loaded:
            return

        from funasr import AutoModel
        from huggingface_hub import snapshot_download

        # Use HuggingFace hub (with mirror) instead of ModelScope
        # ModelScope remote code loading fails with "No module named 'model'"
        model_id = "FunAudioLLM/SenseVoiceSmall"
        logger.info("[FunASR] Loading model: %s (hub=hf) ...", model_id)

        # HACK: Manually download first, then rename requirements.txt to prevent
        # FunASR from trying to pip install dependencies (which hangs/fails due to network).
        try:
            model_dir = snapshot_download(model_id)
            req_file = os.path.join(model_dir, "requirements.txt")
            req_bak = os.path.join(model_dir, "requirements.txt.bak")

            if os.path.exists(req_file):
                logger.info("[FunASR] Renaming requirements.txt to bypass auto-install...")
                # If backup exists (e.g. from previous crash), remove it first to allow rename
                if os.path.exists(req_bak):
                    try:
                        os.remove(req_bak)
                    except Exception as e:
                        logger.warning(f"[FunASR] Failed to remove stale backup: {e}")

                os.rename(req_file, req_bak)
        except Exception as e:
            logger.warning("[FunASR] Failed to pre-process model files: %s", e)
            # Fallback to standard loading if something goes wrong
            model_dir = model_id

        try:
            import torch

            config_device = _read_device()

            if "cuda" in config_device and not torch.cuda.is_available():
                logger.warning("[FunASR] CUDA is not available, falling back to CPU")
                _device = "cpu"
            else:
                _device = config_device

            _model = AutoModel(
                model=model_dir,
                trust_remote_code=True,
                device=_device,
                disable_update=True,
                hub="hf",
            )
        finally:
            # Restore requirements.txt
            if "req_file" in locals() and os.path.exists(req_bak):
                try:
                    if os.path.exists(req_file):
                        try:
                            os.remove(req_file)
                        except Exception:
                            pass
                    os.rename(req_bak, req_file)
                    logger.info("[FunASR] Restored requirements.txt")
                except Exception as e:
                    logger.warning(f"[FunASR] Failed to restore requirements.txt: {e}")

        _loaded = True
        logger.info("[FunASR] Model loaded successfully")


def transcribe(audio_bytes: bytes) -> str:
    """Transcribe audio bytes to text.

    Args:
        audio_bytes: Raw audio file bytes (WAV, MP3, etc.)

    Returns:
        Transcribed text string.
    """
    import tempfile
    import os as _os

    load_model()

    # FunASR requires a file path, so write audio to a temp file
    suffix = ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        results = _model.generate(input=tmp_path, batch_size_s=300)
    finally:
        _os.unlink(tmp_path)

    if not results:
        return ""

    # results is a list of dicts, each with a "text" key
    # SenseVoice may prepend language/emotion tags like <|zh|><|NEUTRAL|><|Speech|><|woitn|>
    text = results[0].get("text", "")

    # Strip SenseVoice special tags (e.g. <|zh|>, <|NEUTRAL|>, <|Speech|>, etc.)
    import re

    text = re.sub(r"<\|[^|]*\|>", "", text).strip()

    return text


def is_loaded() -> bool:
    """Check whether the model has been loaded."""
    return _loaded
