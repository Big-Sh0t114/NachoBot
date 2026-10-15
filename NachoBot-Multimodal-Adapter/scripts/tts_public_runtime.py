"""Avoid loading a second CUDA runtime for a CPU-only Vox classifier on Windows."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import toml

from scripts import tts_runtime_manager as manager

_CPU_TORCH = "torch==2.7.1+cpu"
_PUBLIC_DEPENDENCIES = (
    "aiohttp==3.14.3", "fastapi==0.140.7", "loguru==0.7.3",
    "numpy==2.4.6", "scipy==1.17.1", "toml==0.10.2",
    "transformers==5.14.0", "uvicorn==0.51.0",
)
_PROBE = (
    "import aiohttp,fastapi,loguru,numpy,scipy,toml,transformers,uvicorn,torch; "
    "from transformers import AutoTokenizer,AutoModelForSequenceClassification; "
    "assert torch.__version__ == '2.7.1+cpu'; assert torch.version.cuda is None"
)


def prepare_cpu_public_runtime() -> Path:
    # This directory is separate from both the shared perception environment
    # and the raw VoxCPM CUDA environment. Their packages are never modified.
    root = manager.RUNTIME_ROOT / "public-cpu"
    python = manager.ensure_venv(root)
    marker = python.parent.parent / ".public-cpu-v1.ready"

    def ready() -> bool:
        result = subprocess.run(
            [str(python), "-I", "-c", _PROBE], capture_output=True,
            timeout=60, check=False,
        )
        return result.returncode == 0

    if marker.is_file() and ready():
        return python
    uv = manager.require_uv()
    manager.run([
        uv, "pip", "install", "--python", str(python), _CPU_TORCH,
        "--index-url", "https://download.pytorch.org/whl/cpu",
    ])
    manager.run([uv, "pip", "install", "--python", str(python), *_PUBLIC_DEPENDENCIES])
    if not ready():
        raise RuntimeError("TTS CPU public runtime validation failed")
    marker.write_text("CPU-only Torch and public TTS imports validated\n", encoding="utf-8")
    return python


def resolve_public_python(engine: str, config_path: Path, *, platform: str) -> str:
    if platform != "nt" or engine != "voxcpm":
        return sys.executable
    emotion = toml.load(str(config_path.parent / "vox.toml")).get("emotion", {})
    if not emotion.get("enabled", True) or str(emotion.get("classifier_device", "cpu")) != "cpu":
        return sys.executable
    return str(prepare_cpu_public_runtime())
