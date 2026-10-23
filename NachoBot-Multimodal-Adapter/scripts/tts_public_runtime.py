"""Avoid loading a second CUDA runtime for a CPU-only Vox classifier on Windows."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import toml

from scripts import tts_runtime_manager as manager

_CPU_TORCH = "torch==2.13.0+cpu"
_PUBLIC_PACKAGE_VERSIONS = (
    ("aiohttp", "3.14.5"),
    ("cryptography", "50.0.2"),
    ("fastapi", "0.140.7"),
    ("loguru", "0.7.3"),
    ("numpy", "2.4.6"),
    ("pillow", "13.0.0"),
    ("scipy", "1.17.1"),
    ("toml", "0.10.2"),
    ("transformers", "5.14.0"),
    ("uvicorn", "0.54.0"),
)
_PUBLIC_DEPENDENCIES = tuple(f"{name}=={version}" for name, version in _PUBLIC_PACKAGE_VERSIONS)
_EXPECTED_VERSIONS = {"torch": _CPU_TORCH.split("==", maxsplit=1)[1], **dict(_PUBLIC_PACKAGE_VERSIONS)}
_PROBE = (
    "import importlib.metadata as metadata\n"
    f"expected = {_EXPECTED_VERSIONS!r}\n"
    "actual = {name: metadata.version(name) for name in expected}\n"
    "assert actual == expected, f'public CPU runtime package versions differ: {actual!r}'\n"
    "import aiohttp, cryptography, fastapi, loguru, numpy, scipy, toml, transformers, uvicorn, torch\n"
    "from cryptography.fernet import Fernet\n"
    "from PIL import Image\n"
    "from transformers import AutoTokenizer, AutoModelForSequenceClassification\n"
    "assert torch.__version__ == expected['torch']\n"
    "assert torch.version.cuda is None"
)


def _ready_marker(python: Path) -> Path:
    return python.parent.parent / ".public-cpu-v2.ready"


def _ready_marker_content() -> str:
    recipe = "\n".join((_CPU_TORCH, *_PUBLIC_DEPENDENCIES, _PROBE))
    digest = hashlib.sha256(recipe.encode("utf-8")).hexdigest()
    return f"recipe-sha256={digest}\n"


def _runtime_is_ready(python: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", _PROBE], capture_output=True,
            text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def prepare_cpu_public_runtime() -> Path:
    # This directory is separate from both the shared perception environment
    # and the raw VoxCPM CUDA environment. Their packages are never modified.
    root = manager.RUNTIME_ROOT / "public-cpu"
    python = manager.ensure_venv(root)
    runtime_dir = python.parent.parent
    marker = _ready_marker(python)
    legacy_marker = runtime_dir / ".public-cpu-v1.ready"
    legacy_marker.unlink(missing_ok=True)
    expected_marker = _ready_marker_content()

    if marker.is_file() and marker.read_text(encoding="utf-8") == expected_marker and _runtime_is_ready(python):
        return python
    # A stale marker must not survive a partial install or a failed probe.
    marker.unlink(missing_ok=True)
    uv = manager.require_uv()
    manager.run([
        uv, "pip", "install", "--python", str(python), _CPU_TORCH,
        "--index-url", "https://download.pytorch.org/whl/cpu",
    ])
    manager.run([uv, "pip", "install", "--python", str(python), *_PUBLIC_DEPENDENCIES])
    if not _runtime_is_ready(python):
        raise RuntimeError("TTS CPU public runtime validation failed")
    marker.write_text(expected_marker, encoding="utf-8")
    return python


def resolve_public_python(engine: str, config_path: Path, *, platform: str) -> str:
    if platform != "nt" or engine != "voxcpm":
        return sys.executable
    emotion = toml.load(str(config_path.parent / "vox.toml")).get("emotion", {})
    if not emotion.get("enabled", True) or str(emotion.get("classifier_device", "cpu")) != "cpu":
        return sys.executable
    return str(prepare_cpu_public_runtime())
