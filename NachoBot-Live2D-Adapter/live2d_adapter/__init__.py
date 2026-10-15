"""Standalone Live2D adapter.

Pure model inspection and control helpers are safe to import in headless
processes.  The optional desktop runtime and WebSocket server are imported on
demand so importing :mod:`live2d_adapter.control_pipeline` does not import
pygame or initialize any rendering resources.
"""

from .config import AdapterConfig, ConfigError, ModelAdaptationConfig, load_config
from .control_pipeline import (
    ACTION_TO_CANONICAL_ID,
    ALLOWED_EMOTIONS,
    ApplyOutcome,
    ControlPipeline,
    PreparedReply,
)
from .model_adapter import (
    Live2DModelAdapter,
    ModelAdaptationError,
    ModelMetadata,
    inspect_model,
)
from .protocol import (
    PROTOCOL_VERSION,
    AvatarCommand,
    AvatarEvent,
    AvatarInteraction,
    InteractionEvent,
    ProtocolError,
)
__version__ = "0.1.0"


def __getattr__(name: str):
    """Load desktop-only public classes without burdening library imports."""
    if name == "AvatarRuntime":
        from .runtime import AvatarRuntime

        return AvatarRuntime
    if name == "AvatarWebSocketServer":
        from .server import AvatarWebSocketServer

        return AvatarWebSocketServer
    raise AttributeError(name)

__all__ = [
    "PROTOCOL_VERSION",
    "AdapterConfig",
    "AvatarCommand",
    "AvatarEvent",
    "AvatarInteraction",
    "AvatarRuntime",
    "AvatarWebSocketServer",
    "ACTION_TO_CANONICAL_ID",
    "ALLOWED_EMOTIONS",
    "ApplyOutcome",
    "ConfigError",
    "InteractionEvent",
    "Live2DModelAdapter",
    "ModelAdaptationConfig",
    "ModelAdaptationError",
    "ModelMetadata",
    "ProtocolError",
    "ControlPipeline",
    "PreparedReply",
    "inspect_model",
    "load_config",
]
