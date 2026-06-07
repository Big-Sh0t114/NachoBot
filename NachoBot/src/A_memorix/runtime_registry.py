from __future__ import annotations

from typing import Any, Dict, Optional

_runtime_kernel: Any = None


def set_runtime_kernel(kernel: Any | None) -> None:
    global _runtime_kernel
    _runtime_kernel = kernel


def get_runtime_kernel() -> Any | None:
    return _runtime_kernel


def get_runtime_components() -> Dict[str, Any]:
    kernel = get_runtime_kernel()
    if kernel is None:
        return {}
    return {
        "vector_store": getattr(kernel, "vector_store", None),
        "graph_store": getattr(kernel, "graph_store", None),
        "metadata_store": getattr(kernel, "metadata_store", None),
        "embedding_manager": getattr(kernel, "embedding_manager", None),
        "sparse_index": getattr(kernel, "sparse_index", None),
    }
