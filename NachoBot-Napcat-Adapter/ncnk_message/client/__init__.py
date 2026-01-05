"""
WebSocket Client Module - High-level WebSocket client components

This module provides easy-to-use interfaces for building WebSocket clients
with the ncnk_message library.
"""

# Core client classes
from ..client_ws_api import WebSocketClient
from ..multi_client import WebSocketMultiClient

# Factory functions for config
from ..client_factory import create_client_config, create_ssl_client_config

# Message and data structures (from message module)
from ..message import (
    APIMessageBase,
    MessageDim,
    Seg,
    BaseMessageInfo,
)

# Configuration and utilities
from ..ws_config import (
    ClientConfig,
    create_client_config,
    create_ssl_client_config,
)

# All exports
__all__ = [
    # Core Clients
    "WebSocketClient",          # 单连接客户端（主要使用）
    "WebSocketMultiClient",     # 多连接客户端（特殊情况使用）

    # Factory Functions for Config
    "create_client_config",     # 创建单连接客户端配置的便捷函数
    "create_ssl_client_config", # 创建SSL客户端配置的便捷函数

    # Message Classes (API-Server Version)
    "APIMessageBase",           # 主要消息类
    "MessageDim",
    "Seg",
    "BaseMessageInfo",

    # Configuration
    "ClientConfig",
    "create_client_config",
    "create_ssl_client_config",
]