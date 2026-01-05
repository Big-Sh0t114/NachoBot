"""
WebSocket Module - Complete WebSocket networking components

This module provides all WebSocket-related classes and utilities for both
server and client implementations.
"""

# Server components
from ..server_ws_api import WebSocketServer
from ..server_ws_connection import ServerNetworkDriver

# Client components
from ..client_ws_api import WebSocketClient
from ..client_ws_connection import ClientNetworkDriver

# Message and data structures (from message module)
from ..message import (
    APIMessageBase,
    MessageDim,
    Seg,
    BaseMessageInfo,
    GroupInfo,
    UserInfo,
    InfoBase,
    SenderInfo,
    ReceiverInfo,
    FormatInfo,
    TemplateInfo,
)


# Configuration and utilities
from ..ws_config import (
    ServerConfig,
    ClientConfig,
    AuthResult,
    ConfigManager,
    create_server_config,
    create_client_config,
)

# All exports
__all__ = [
    # Server Components
    "WebSocketServer",
    "ServerNetworkDriver",

    # Client Components
    "WebSocketClient",
    "ClientNetworkDriver",

    # Message Classes (API-Server Version)
    "APIMessageBase",          # 主要消息类
    "MessageDim",
    "Seg",
    "BaseMessageInfo",
    "GroupInfo",
    "UserInfo",
    "InfoBase",
    "SenderInfo",
    "ReceiverInfo",
    "FormatInfo",
    "TemplateInfo",

    # Configuration
    "ServerConfig",
    "ClientConfig",
    "AuthResult",
    "ConfigManager",
    "create_server_config",
    "create_client_config",
]