"""Lightweight canonical TTS/VLM device configuration for the adapter.

This module intentionally uses only the Python standard library. It is safe to
import before Torch and does not depend on the WebUI or its GPU inventory.
"""

from __future__ import annotations

import csv
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Mapping


ADAPTER_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = ADAPTER_ROOT / "configs"
GPU_UUID_PATTERN = re.compile(r"^GPU-[A-Za-z0-9-]+$")
CUDA_DEVICE_PATTERN = re.compile(r"^cuda(?::[0-9]+)?$")
DEFAULT_TTS_DEVICE = "auto"
DEFAULT_VLM_DEVICE = "auto"


class ModelDeviceConfigError(ValueError):
    """Malformed, unsupported, or unavailable canonical model-device choice."""


def adapter_root(path: Path | str | None = None) -> Path:
    """Return an adapter root from a root/config path, or this module's root."""
    if path is None:
        return ADAPTER_ROOT
    value = Path(path).expanduser().resolve(strict=False)
    if value.name == "configs":
        return value.parent
    if value.is_file() or value.suffix.lower() == ".toml":
        return value.parent.parent if value.parent.name == "configs" else value.parent
    if value.name == "NachoBot-Multimodal-Adapter":
        return value
    if (value / "NachoBot-Multimodal-Adapter").is_dir():
        return value / "NachoBot-Multimodal-Adapter"
    return value


def config_paths(
    *,
    root_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    base_path: Path | str | None = None,
    perception_path: Path | str | None = None,
) -> tuple[Path, Path]:
    """Resolve canonical base/perception files from explicit config/root paths."""
    if config_dir is not None:
        directory = Path(config_dir).expanduser().resolve(strict=False)
    elif root_dir is not None:
        directory = adapter_root(root_dir) / "configs"
    else:
        directory = DEFAULT_CONFIG_DIR
    base = Path(base_path).expanduser().resolve(strict=False) if base_path else directory / "base.toml"
    perception = (
        Path(perception_path).expanduser().resolve(strict=False)
        if perception_path
        else directory / "perception.toml"
    )
    return base, perception


def validate_device_choice(model: str, value: object) -> str:
    """Validate one typed device value without changing legacy VLM CUDA strings."""
    if model not in {"tts", "vlm"}:
        raise ModelDeviceConfigError(f"unknown model device key: {model}")
    if not isinstance(value, str):
        field = "[model_devices].tts" if model == "tts" else "[perception.device].vlm"
        raise ModelDeviceConfigError(f"{field} 必须是字符串")
    if value in {"auto", "cpu"} or GPU_UUID_PATTERN.fullmatch(value):
        return value
    if model == "vlm" and CUDA_DEVICE_PATTERN.fullmatch(value):
        return value
    if model == "tts":
        raise ModelDeviceConfigError("[model_devices].tts 必须为 'auto'、'cpu' 或完整 NVIDIA GPU UUID")
    raise ModelDeviceConfigError(
        "[perception.device].vlm 必须为 'auto'、'cpu'、完整 NVIDIA GPU UUID、'cuda' 或 'cuda:N'"
    )


def _read_toml(path: Path, *, label: str) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ModelDeviceConfigError(f"cannot read {label} TOML: {path}") from exc
    if not isinstance(document, dict):
        raise ModelDeviceConfigError(f"{label} TOML root must be a table")
    return document


def read_device_choice(
    model: str,
    *,
    root_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    base_path: Path | str | None = None,
    perception_path: Path | str | None = None,
) -> str:
    """Read one canonical choice without parsing the other model's config."""
    if model not in {"tts", "vlm"}:
        raise ModelDeviceConfigError(f"unknown model device key: {model}")
    base_file, perception_file = config_paths(
        root_dir=root_dir,
        config_dir=config_dir,
        base_path=base_path,
        perception_path=perception_path,
    )
    if model == "tts":
        base = _read_toml(base_file, label="base")
        model_devices = base.get("model_devices", {})
        if not isinstance(model_devices, dict):
            raise ModelDeviceConfigError("[model_devices] must be a TOML table")
        return validate_device_choice("tts", model_devices.get("tts", DEFAULT_TTS_DEVICE))

    perception = _read_toml(perception_file, label="perception")
    perception_section = perception.get("perception", {})
    if not isinstance(perception_section, dict):
        raise ModelDeviceConfigError("[perception] must be a TOML table")
    device_section = perception_section.get("device", {})
    if not isinstance(device_section, dict):
        raise ModelDeviceConfigError("[perception.device] must be a TOML table")
    return validate_device_choice("vlm", device_section.get("vlm", DEFAULT_VLM_DEVICE))


def read_model_devices(
    *,
    root_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    base_path: Path | str | None = None,
    perception_path: Path | str | None = None,
) -> dict[str, str]:
    """Read and validate both canonical choices; missing fields use defaults."""
    paths = {
        "root_dir": root_dir,
        "config_dir": config_dir,
        "base_path": base_path,
        "perception_path": perception_path,
    }
    return {model: read_device_choice(model, **paths) for model in ("tts", "vlm")}


def nvidia_gpu_uuids() -> list[str]:
    """Return detected NVIDIA GPU UUIDs in ``nvidia-smi`` index order."""
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModelDeviceConfigError("cannot verify the configured NVIDIA GPU UUID: nvidia-smi is unavailable") from exc
    if completed.returncode != 0:
        raise ModelDeviceConfigError("cannot verify the configured NVIDIA GPU UUID: nvidia-smi failed")

    uuids: list[str] = []
    for row in csv.reader(completed.stdout.splitlines(), skipinitialspace=True):
        if row:
            value = row[0].strip()
            if GPU_UUID_PATTERN.fullmatch(value):
                uuids.append(value)
    return uuids


def _nvidia_uuids() -> set[str]:
    return set(nvidia_gpu_uuids())


def detected_nvidia_uuids() -> set[str]:
    """Return NVIDIA UUIDs when detection works, or an empty set otherwise.

    Automatic launcher planning treats an unavailable ``nvidia-smi`` as a
    CPU-only host. Explicit UUID selections still use :func:`_nvidia_uuids`
    directly so an unverifiable choice fails before dependency preparation.
    """
    try:
        return _nvidia_uuids()
    except ModelDeviceConfigError:
        return set()


def detected_nvidia_gpu_uuids() -> list[str]:
    """Return ordered NVIDIA UUIDs when detection works, or an empty list."""
    try:
        return nvidia_gpu_uuids()
    except ModelDeviceConfigError:
        return []


def auto_cuda_available(
    *,
    environ: Mapping[str, str] | None = None,
    gpu_uuids: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Whether an automatic local model can use a visible NVIDIA device.

    CUDA uses the valid prefix of ``CUDA_VISIBLE_DEVICES``: an invalid item
    ends the list, while valid selectors before it remain visible. Numeric
    selectors are checked against the ordered GPU inventory; UUID selectors
    accept only a full or unique UUID prefix.
    """
    current = os.environ if environ is None else environ
    visible = current.get("CUDA_VISIBLE_DEVICES")
    if visible is not None and str(visible).strip() in {"", "-1"}:
        return False
    inventory = detected_nvidia_gpu_uuids() if gpu_uuids is None else list(gpu_uuids)
    if not inventory:
        return False
    if visible is None:
        return True

    selectors = str(visible).split(",")
    has_visible_prefix = False
    for selector in selectors:
        value = selector.strip()
        if not value:
            break
        if value.isdecimal():
            if int(value) >= len(inventory):
                break
        elif value.casefold().startswith("gpu-"):
            prefix = value.casefold()
            if sum(uuid.casefold().startswith(prefix) for uuid in inventory) != 1:
                break
        else:
            break
        has_visible_prefix = True
    return has_visible_prefix


def device_environment(
    model: str,
    choice: str,
    *,
    runtime_profile: str = "gpu",
    verify_gpu: bool = True,
) -> dict[str, str]:
    """Map a validated choice to process environment overrides."""
    value = validate_device_choice(model, choice)
    if value == "auto" or (model == "vlm" and CUDA_DEVICE_PATTERN.fullmatch(value)):
        return {}

    profile = str(runtime_profile or "gpu").strip().casefold()
    is_gpu = bool(GPU_UUID_PATTERN.fullmatch(value))
    if profile == "cpu" and is_gpu:
        raise ModelDeviceConfigError(
            f"当前启动的是 CPU Multimodal 环境，{model.upper()} 已指定 NVIDIA GPU；"
            "请将该模型改为自动/CPU，或改用 GPU 环境"
        )
    if value == "cpu":
        overrides = {"CUDA_VISIBLE_DEVICES": ""}
        if model == "vlm":
            overrides["NACHOBOT_VLM_DEVICE"] = "cpu"
        return overrides

    if is_gpu:
        if verify_gpu and value not in _nvidia_uuids():
            raise ModelDeviceConfigError(
                f"configured NVIDIA GPU {value} is unavailable; choose another GPU or set the model to auto/CPU"
            )
        overrides = {"CUDA_VISIBLE_DEVICES": value}
        if model == "vlm":
            overrides["NACHOBOT_VLM_DEVICE"] = "cuda:0"
        return overrides
    raise ModelDeviceConfigError(f"unsupported {model.upper()} device choice: {value}")


def apply_device_environment(
    model: str,
    *,
    root_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    base_path: Path | str | None = None,
    perception_path: Path | str | None = None,
    runtime_profile: str = "gpu",
) -> dict[str, str]:
    """Apply the canonical choice to this process and return the overrides."""
    choice = read_device_choice(
        model,
        root_dir=root_dir,
        config_dir=config_dir,
        base_path=base_path,
        perception_path=perception_path,
    )
    overrides = device_environment(
        model,
        choice,
        runtime_profile=runtime_profile,
    )
    os.environ.update(overrides)
    return overrides


def vlm_torch_device(
    *,
    root_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    perception_path: Path | str | None = None,
    runtime_profile: str = "gpu",
) -> str:
    """Apply VLM visibility and return a safe torch device string."""
    choice = read_device_choice(
        "vlm",
        root_dir=root_dir,
        config_dir=config_dir,
        perception_path=perception_path,
    )
    overrides = device_environment("vlm", choice, runtime_profile=runtime_profile)
    os.environ.update(overrides)
    if choice == "auto":
        return os.environ.get("NACHOBOT_VLM_DEVICE", "").strip() or "cuda"
    if GPU_UUID_PATTERN.fullmatch(choice):
        return "cuda:0"
    return choice
