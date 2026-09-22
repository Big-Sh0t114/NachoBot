"""Standalone Live2D rendering adapter for NachoBot."""

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
from .runtime import AvatarRuntime
from .server import AvatarWebSocketServer

__version__ = "0.1.0"

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
