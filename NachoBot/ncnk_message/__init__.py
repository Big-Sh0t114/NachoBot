"""Nacho Message - A message handling library"""

__version__ = "0.6.1"

# Legacy API Components (pre-API-Server Version) - 从根模块导入
from .api import MessageClient, MessageServer
from .router import Router, RouteConfig, TargetConfig, get_core_token_from_env
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
from .system_event import (
    SYSTEM_EVENT_KEY,
    SYSTEM_EVENT_ROUTE_KEY,
    SYSTEM_EVENT_ROUTE_KIND_PRIVATE,
    SYSTEM_EVENT_VERSION,
    SystemEventClassification,
    SystemEventResult,
    SystemEventRouteClassification,
    SystemEventRouteResult,
    SystemEventRouteStatus,
    SystemEventState,
    SystemEventStatus,
    build_system_event,
    build_system_event_route,
    classify_system_event,
    classify_system_event_route,
    get_system_event,
    get_system_event_route,
    system_event_fallback_text,
    system_event_result,
    system_event_route_result,
    validate_system_event_route,
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
    "get_core_token_from_env",
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
    "SYSTEM_EVENT_KEY",
    "SYSTEM_EVENT_ROUTE_KEY",
    "SYSTEM_EVENT_ROUTE_KIND_PRIVATE",
    "SYSTEM_EVENT_VERSION",
    "SystemEventState",
    "SystemEventStatus",
    "SystemEventResult",
    "SystemEventClassification",
    "SystemEventRouteResult",
    "SystemEventRouteStatus",
    "SystemEventRouteClassification",
    "build_system_event",
    "build_system_event_route",
    "classify_system_event",
    "classify_system_event_route",
    "system_event_result",
    "system_event_route_result",
    "get_system_event",
    "get_system_event_route",
    "validate_system_event_route",
    "system_event_fallback_text",
    # 注意：API-Server Version 组件需要从子模块导入：
    # - 消息相关: from ncnk_message.message import APIMessageBase, MessageDim, etc.
    # - 服务端: from ncnk_message.server import WebSocketServer, ServerConfig, etc.
    # - 客户端: from ncnk_message.client import WebSocketClient, ClientConfig, etc.
]
