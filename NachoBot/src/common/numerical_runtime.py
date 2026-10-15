"""Configure numerical workers before their native libraries are imported."""

import os


def configure_numerical_runtime() -> None:
    # Windows OpenBLAS builds reserve large private work buffers per worker
    # during import, including Faiss' own OpenBLAS copy. Core performs small
    # vector queries; bound the default before NumPy/Faiss/SciPy load. Honour
    # deployment overrides, including those already loaded from .env.
    if os.name == "nt":
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
