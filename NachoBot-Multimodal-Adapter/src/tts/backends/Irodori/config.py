"""Small, dependency-free config loader for the managed Irodori backend."""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import Any


DEFAULT_HF_CHECKPOINT = "Aratako/Irodori-TTS-v4-Large"
DEFAULT_CODEC_REPO = "Aratako/Semantic-DACVAE-Japanese-32dim"
MAX_TEXT_LENGTH = 10_000
MAX_AUDIO_SECONDS = 120.0
MAX_AUDIO_BYTES = 32 * 1024 * 1024
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9881


class IrodoriConfigError(ValueError):
    """The selected Irodori configuration is incomplete or invalid."""


def _table(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise IrodoriConfigError(f"[{key}] must be a TOML table")
    return value


def _finite_number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IrodoriConfigError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise IrodoriConfigError(f"{name} must be between {minimum:g} and {maximum:g}")
    return result


def _positive_int(value: Any, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise IrodoriConfigError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _optional_path(value: Any, name: str, config_dir: Path) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise IrodoriConfigError(f"{name} must be a path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return path.resolve(strict=False)


def _path_or_repo(value: Any, name: str, config_dir: Path, default: str) -> str:
    if value in (None, ""):
        return default
    if not isinstance(value, str) or not value.strip():
        raise IrodoriConfigError(f"{name} must be a non-empty string")
    path = Path(value).expanduser()
    # Hugging Face repository IDs use ``owner/name``. Relative local paths are
    # made explicit with ./ or ../ so they cannot be mistaken for repo IDs.
    if path.is_absolute() or value.startswith(("./", "../", ".\\", "..\\")):
        if not path.is_absolute():
            path = config_dir / path
        return str(path.resolve(strict=False))
    return value.strip()


def load_irodori_config(path: str | Path) -> dict[str, Any]:
    """Load and validate config; resolve configured local paths beside it."""

    config_path = Path(path).expanduser().resolve(strict=False)
    try:
        with config_path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise IrodoriConfigError(f"cannot read Irodori config: {config_path}") from exc
    if not isinstance(document, dict):
        raise IrodoriConfigError("Irodori TOML root must be a table")

    config_dir = config_path.parent
    runtime_in = _table(document, "runtime")
    tts_in = _table(document, "tts")
    generation_in = _table(document, "generation")

    host = tts_in.get("host", DEFAULT_HOST)
    if not isinstance(host, str) or host.strip().lower() not in {"127.0.0.1", "localhost"}:
        raise IrodoriConfigError("[tts].host must bind to loopback (127.0.0.1 or localhost)")
    port = _positive_int(tts_in.get("port", DEFAULT_PORT), "[tts].port", maximum=65535)
    if port == 9880:
        raise IrodoriConfigError("Irodori raw API cannot bind the public 9880 port")
    timeout_seconds = _finite_number(
        tts_in.get("timeout_seconds", 120), "[tts].timeout_seconds", minimum=1, maximum=120
    )

    repo_path = _optional_path(runtime_in.get("repo_path", ""), "[runtime].repo_path", config_dir)
    python_path = _optional_path(runtime_in.get("python_path", ""), "[runtime].python_path", config_dir)
    checkpoint = _optional_path(runtime_in.get("checkpoint", ""), "[runtime].checkpoint", config_dir)
    if checkpoint is not None and not checkpoint.is_file():
        raise IrodoriConfigError(f"configured Irodori checkpoint does not exist: {checkpoint}")
    hf_checkpoint = runtime_in.get("hf_checkpoint", DEFAULT_HF_CHECKPOINT)
    if not isinstance(hf_checkpoint, str):
        raise IrodoriConfigError("[runtime].hf_checkpoint must be a string")
    hf_checkpoint = hf_checkpoint.strip()
    if checkpoint is None and not hf_checkpoint:
        raise IrodoriConfigError("set either [runtime].checkpoint or [runtime].hf_checkpoint")
    if checkpoint is not None and hf_checkpoint and hf_checkpoint != DEFAULT_HF_CHECKPOINT:
        raise IrodoriConfigError("set only one of [runtime].checkpoint and [runtime].hf_checkpoint")
    if checkpoint is not None:
        hf_checkpoint = ""

    model_device = runtime_in.get("model_device", "auto")
    codec_device = runtime_in.get("codec_device", "auto")
    for key, value in (("model_device", model_device), ("codec_device", codec_device)):
        if not isinstance(value, str) or not value.strip():
            raise IrodoriConfigError(f"[runtime].{key} must be a non-empty device string")
    model_precision = runtime_in.get("model_precision", "auto")
    codec_precision = runtime_in.get("codec_precision", "auto")
    for key, value in (("model_precision", model_precision), ("codec_precision", codec_precision)):
        if not isinstance(value, str) or value not in {"auto", "fp32", "bf16"}:
            raise IrodoriConfigError(f"[runtime].{key} must be auto, fp32, or bf16")
    codec_repo = _path_or_repo(
        runtime_in.get("codec_repo", DEFAULT_CODEC_REPO),
        "[runtime].codec_repo",
        config_dir,
        DEFAULT_CODEC_REPO,
    )
    # Deprecated legacy setting. Keep accepting old configs, but shared GPU/
    # CPU profile selection now owns the Torch wheel family.
    extra = runtime_in.get("extra", "auto")
    if not isinstance(extra, str) or extra not in {"auto", "cpu", "cu128"}:
        raise IrodoriConfigError("[runtime].extra must be auto, cpu, or cu128")
    hf_home = _optional_path(runtime_in.get("hf_home", ""), "[runtime].hf_home", config_dir)

    no_ref = generation_in.get("no_ref", True)
    if not isinstance(no_ref, bool):
        raise IrodoriConfigError("[generation].no_ref must be a boolean")
    ref_wav = _optional_path(generation_in.get("ref_wav", ""), "[generation].ref_wav", config_dir)
    lora_adapter = _optional_path(
        generation_in.get("lora_adapter", ""), "[generation].lora_adapter", config_dir
    )
    for name, local_path, want_dir in (
        ("[generation].ref_wav", ref_wav, False),
        ("[generation].lora_adapter", lora_adapter, True),
    ):
        if local_path is not None and not (local_path.is_dir() if want_dir else local_path.is_file()):
            raise IrodoriConfigError(f"configured {name} does not exist: {local_path}")
    if no_ref and ref_wav is not None:
        raise IrodoriConfigError("[generation].ref_wav requires no_ref = false")
    if not no_ref and ref_wav is None:
        raise IrodoriConfigError("set [generation].ref_wav or use no_ref = true")

    caption = generation_in.get("caption", "")
    if not isinstance(caption, str) or len(caption) > MAX_TEXT_LENGTH:
        raise IrodoriConfigError("[generation].caption must be a string up to 10000 characters")
    caption = caption.strip() or None
    num_steps = generation_in.get("num_steps")
    if num_steps is not None:
        num_steps = _positive_int(num_steps, "[generation].num_steps", maximum=200)
    seed = generation_in.get("seed")
    if seed is not None and (
        isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**63
    ):
        raise IrodoriConfigError("[generation].seed must be a non-negative integer or omitted")

    duration_scale = _finite_number(
        generation_in.get("duration_scale", 1.0), "[generation].duration_scale", minimum=0.1, maximum=10
    )
    min_seconds = _finite_number(
        generation_in.get("min_seconds", 0.5), "[generation].min_seconds", minimum=0.1, maximum=120
    )
    max_seconds = _finite_number(
        generation_in.get("max_seconds", 30.0), "[generation].max_seconds", minimum=0.1, maximum=120
    )
    if min_seconds > max_seconds:
        raise IrodoriConfigError("[generation].min_seconds cannot exceed max_seconds")
    cfg_scales = {
        name: _finite_number(generation_in.get(name, default), f"[generation].{name}", minimum=0, maximum=20)
        for name, default in (
            ("cfg_scale_text", 3.0),
            ("cfg_scale_caption", 3.0),
            ("cfg_scale_speaker", 5.0),
        )
    }

    return {
        "config_path": config_path,
        "runtime": {
            "repo_path": repo_path,
            "python_path": python_path,
            "checkpoint": checkpoint,
            "hf_checkpoint": hf_checkpoint,
            "model_device": model_device.strip(),
            "model_precision": model_precision,
            "codec_device": codec_device.strip(),
            "codec_precision": codec_precision,
            "codec_repo": codec_repo,
            "extra": extra,
            "hf_home": hf_home,
        },
        "tts": {"host": host.strip(), "port": port, "timeout_seconds": timeout_seconds},
        "generation": {
            "no_ref": no_ref,
            "ref_wav": ref_wav,
            "lora_adapter": lora_adapter,
            "caption": caption,
            "num_steps": num_steps,
            "seed": seed,
            "duration_scale": duration_scale,
            "min_seconds": min_seconds,
            "max_seconds": max_seconds,
            **cfg_scales,
        },
    }


def sampling_request_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    """Map trusted config settings to the upstream SamplingRequest fields."""

    generation = config["generation"]
    return {
        "caption": generation["caption"],
        "ref_wav": str(generation["ref_wav"]) if generation["ref_wav"] else None,
        "no_ref": generation["no_ref"],
        "lora_adapter": str(generation["lora_adapter"]) if generation["lora_adapter"] else None,
        "num_steps": generation["num_steps"],
        "seed": generation["seed"],
        "duration_scale": generation["duration_scale"],
        "min_seconds": generation["min_seconds"],
        "max_seconds": generation["max_seconds"],
        "cfg_scale_text": generation["cfg_scale_text"],
        "cfg_scale_caption": generation["cfg_scale_caption"],
        "cfg_scale_speaker": generation["cfg_scale_speaker"],
    }


__all__ = [
    "DEFAULT_CODEC_REPO",
    "DEFAULT_HF_CHECKPOINT",
    "IrodoriConfigError",
    "load_irodori_config",
    "sampling_request_kwargs",
]
