"""Standalone Live2D rendering adapter for NachoBot."""

import os

# Keep machine-readable CLI output (for example --print-launch-config) free of
# pygame's import-time greeting while leaving normal logging unchanged.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from .config import (
    AdapterConfig,
    ConfigError,
    ModelAdaptationConfig,
    RuntimeConfig,
    load_config,
)
from .action_adapter import ActionAdapter, ActionDecision
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
    "ActionAdapter",
    "ActionDecision",
    "AvatarCommand",
    "AvatarEvent",
    "AvatarInteraction",
    "AvatarRuntime",
    "AvatarWebSocketServer",
    "ConfigError",
    "InteractionEvent",
    "Live2DModelAdapter",
    "ModelAdaptationConfig",
    "ModelAdaptationError",
    "ModelMetadata",
    "ProtocolError",
    "RuntimeConfig",
    "inspect_model",
    "load_config",
]
