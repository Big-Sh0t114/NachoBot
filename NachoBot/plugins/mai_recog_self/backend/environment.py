"""Environment shared by all isolated self-recognition worker processes."""

from __future__ import annotations

import os
from pathlib import Path


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
