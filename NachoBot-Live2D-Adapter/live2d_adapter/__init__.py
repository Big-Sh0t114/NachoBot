"""Independent Live2D model rendering provider.

Importing this package does not initialize the Live2D SDK, pygame, or a window.
The host creates and owns the OpenGL context, then composes a
``Live2DModelRuntime`` into its render loop.
"""

from .model_adapter import Live2DModelAdapter, ModelMetadata, inspect_model
from .model_runtime import Live2DModelRuntime, ModelInfo, RuntimeFrame, RuntimeOptions

__all__ = [
    "Live2DModelAdapter",
    "Live2DModelRuntime",
    "ModelInfo",
    "ModelMetadata",
    "RuntimeFrame",
    "RuntimeOptions",
    "inspect_model",
]
