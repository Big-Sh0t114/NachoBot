"""Nacho Message - A message handling library"""

__version__ = "0.6.1"

# Legacy API Components (pre-API-Server Version) - 从根模块导入
from .api import MessageClient, MessageServer
from .router import Router, RouteConfig, TargetConfig
from .message_base import (
    Seg,
    GroupInfo,
    UserInfo,
    FormatInfo,
    TemplateInfo,
    BaseMessageInfo,
    MessageBase,
    InfoBase,
    SenderInfo,
    ReceiverInfo,
)

# API-Server Version Components 不在根模块导出，需要从子模块导入
# 消息相关组件 - 使用 from ncnk_message.message import
# WebSocket服务端组件 - 使用 from ncnk_message.server import
# WebSocket客户端组件 - 使用 from ncnk_message.client import
# 新的专用客户端 - 使用 from ncnk_message.simple_client import 和 from ncnk_message.multi_client import

__all__ = [
    # Legacy API Components (从根模块导入)
    "MessageClient",
    "MessageServer",
    "Router",
    "RouteConfig",
    "TargetConfig",
    "MessageBase",
    "Seg",
    "GroupInfo",
    "UserInfo",
    "FormatInfo",
    "TemplateInfo",
    "BaseMessageInfo",
    "InfoBase",
    "SenderInfo",
    "ReceiverInfo",
    # 注意：API-Server Version 组件需要从子模块导入：
    # - 消息相关: from ncnk_message.message import APIMessageBase, MessageDim, etc.
    # - 服务端: from ncnk_message.server import WebSocketServer, ServerConfig, etc.
    # - 客户端: from ncnk_message.client import WebSocketClient, ClientConfig, etc.
]
