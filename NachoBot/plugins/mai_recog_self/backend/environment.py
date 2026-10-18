"""Environment shared by all isolated self-recognition worker processes."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from .paths import CORE_ROOT


WORKER_PROBE_TIMEOUT = 20.0
_FRAMEWORK_PROBE = "import torch, torchvision; print(torch.__version__, torchvision.__version__)"


def _absolute_path(path: str | Path) -> Path:
    """Make an interpreter path absolute without dereferencing venv symlinks."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _multimodal_python(core_root: Path | None = None, *, platform: str | None = None) -> Path:
    """Return the sibling adapter's platform-specific venv interpreter path."""
    root = _absolute_path(core_root or CORE_ROOT)
    adapter = root.parent / "NachoBot-Multimodal-Adapter" / ".venv"
    target_platform = platform or sys.platform
    if target_platform.startswith("win"):
        return adapter / "Scripts" / "python.exe"
    return adapter / "bin" / "python"


def _usable_multimodal_python(interpreter: Path, root: Path) -> bool:
    if not interpreter.is_file():
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            [str(interpreter), "-c", _FRAMEWORK_PROBE],
            cwd=CORE_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=WORKER_PROBE_TIMEOUT,
            creationflags=flags,
            env=worker_environment(root),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def select_worker_python(root: Path, explicit: str = "") -> str:
    """Select explicit, healthy sibling Multimodal, or the current Core Python.

    Catalog-persisted paths are deliberately not consulted: catalogs move between
    checkouts and may contain interpreter paths from another machine.
    """
    if explicit:
        candidate = shutil.which(explicit) or explicit
        path = _absolute_path(candidate)
        if not path.is_file():
            raise ValueError(f"worker Python不存在：{path}")
        return str(path)

    multimodal = _multimodal_python()
    if _usable_multimodal_python(multimodal, root):
        return str(multimodal)

    return str(_absolute_path(sys.executable))


def worker_environment(root: Path) -> dict[str, str]:
    """Clear transient Python overlays and expose this catalog's durable extras."""
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.upper() in {"PYTHONPATH", "PYTHONHOME"}:
            environment.pop(name, None)

    dependencies = (Path(root).expanduser().resolve() / "worker_deps").resolve()
    if dependencies.is_dir():
        environment["PYTHONPATH"] = str(dependencies)
    return environment
