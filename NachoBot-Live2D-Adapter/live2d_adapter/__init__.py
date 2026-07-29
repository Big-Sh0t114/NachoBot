"""Standalone Live2D rendering adapter for NachoBot."""

from .config import AdapterConfig, ConfigError, load_config
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
    "ConfigError",
    "InteractionEvent",
    "ProtocolError",
    "load_config",
]
