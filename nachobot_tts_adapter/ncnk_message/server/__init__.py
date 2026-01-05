"""
WebSocket Server Module - High-level WebSocket server components

This module provides easy-to-use interfaces for building WebSocket servers
with the ncnk_message library.
"""

# Core server classes
from ..server_ws_api import WebSocketServer

# Import from the new message module (API-Server Version)
from ..message import (
    # Core message classes (renamed for clarity)
    APIMessageBase,         # Was ServerMessageBase
    MessageDim,
    BaseMessageInfo,
    Seg,

    # Message segment types
    GroupInfo,
    UserInfo,

    # Info base classes
    InfoBase,
    SenderInfo,
    ReceiverInfo,
    FormatInfo,
    TemplateInfo,
)


# Configuration and utilities
from ..ws_config import (
    ServerConfig,
    AuthResult,
    ConfigManager,
    create_server_config,
    create_ssl_server_config,
)

# All exports
__all__ = [
    # Core Server
    "WebSocketServer",

    # Core Message Classes (API-Server Version)
    "APIMessageBase",          # 主要消息类
    "MessageDim",
    "BaseMessageInfo",
    "Seg",

    # Message Segment Types
    "GroupInfo",
    "UserInfo",

    # Info Base Classes
    "InfoBase",
    "SenderInfo",
    "ReceiverInfo",
    "FormatInfo",
    "TemplateInfo",

    # Configuration
    "ServerConfig",
    "AuthResult",
    "ConfigManager",
    "create_server_config",
    "create_ssl_server_config",
]