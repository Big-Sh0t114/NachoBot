"""
API-Server Version Message Module - 最新消息格式和组件

This module contains the latest message classes and components designed
for the API-Server Version WebSocket architecture.
"""

# 所有消息相关的核心类都从api_message_base导入
from ..api_message_base import (
    # 核心消息类
    APIMessageBase,         # 主要消息类（原ServerMessageBase）
    MessageDim,             # 消息维度信息
    BaseMessageInfo,        # 消息基础信息
    Seg,                    # 消息片段

    # 信息类
    InfoBase,               # 信息基类
    GroupInfo,              # 群组信息
    UserInfo,               # 用户信息
    SenderInfo,             # 发送者信息
    ReceiverInfo,           # 接收者信息
    FormatInfo,             # 格式信息
    TemplateInfo,           # 模板信息
)


__all__ = [
    # Core Message Classes
    "APIMessageBase",
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
]