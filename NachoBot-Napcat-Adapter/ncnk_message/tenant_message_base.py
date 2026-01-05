"""
租户感知的消息基类扩展
"""

from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional
from .message_base import MessageBase, BaseMessageInfo


@dataclass
class TenantMessageBase(MessageBase):
    """支持租户的消息基类"""

    tenant_info: Optional[Dict[str, Any]] = None  # 租户信息容器
    connection_uuid: Optional[str] = None  # 连接UUID
    tenant_id: Optional[str] = None  # 租户ID
    api_key: Optional[str] = None  # API密钥
    metadata: Optional[Dict[str, Any]] = None  # 元数据

    def to_dict(self) -> Dict:
        """转换为字典，包含租户信息"""
        result = super().to_dict()

        # 只包含非空的租户字段
        if self.tenant_info is not None:
            result["tenant_info"] = self.tenant_info
        if self.connection_uuid is not None:
            result["connection_uuid"] = self.connection_uuid
        if self.tenant_id is not None:
            result["tenant_id"] = self.tenant_id
        if self.api_key is not None:
            result["api_key"] = self.api_key
        if self.metadata is not None:
            result["metadata"] = self.metadata

        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "TenantMessageBase":
        """从字典创建租户消息实例"""
        # 创建基类实例
        base_instance = MessageBase.from_dict(data)

        # 转换为租户消息
        return cls(
            message_info=base_instance.message_info,
            message_segments=base_instance.message_segments,
            raw_message=base_instance.raw_message,
            tenant_info=data.get("tenant_info"),
            connection_uuid=data.get("connection_uuid"),
            tenant_id=data.get("tenant_id"),
            api_key=data.get("api_key"),
            metadata=data.get("metadata"),
        )


class TenantBaseMessageInfo(BaseMessageInfo):
    """支持租户的基础消息信息"""

    # 新增：完全可选的租户相关信息
    tenant_info: Optional[Dict[str, Any]] = None  # 租户信息容器
    connection_uuid: Optional[str] = None  # 连接UUID
    tenant_id: Optional[str] = None  # 租户ID
    api_key: Optional[str] = None  # API密钥
    metadata: Optional[Dict[str, Any]] = None  # 元数据

    def to_dict(self) -> Dict:
        """转换为字典，只包含非空的租户字段"""
        result = asdict(self)

        # 只包含非空的租户字段
        if self.tenant_info is None:
            result.pop("tenant_info", None)
        if self.connection_uuid is None:
            result.pop("connection_uuid", None)
        if self.tenant_id is None:
            result.pop("tenant_id", None)
        if self.api_key is None:
            result.pop("api_key", None)
        if self.metadata is None:
            result.pop("metadata", None)

        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "TenantBaseMessageInfo":
        """从字典创建租户消息信息实例"""
        return cls(
            platform=data.get("platform"),
            message_id=data.get("message_id"),
            time=data.get("time"),
            sender_info=data.get("sender_info"),
            receiver_info=data.get("receiver_info"),
            user_info=data.get("user_info"),
            group_info=data.get("group_info"),
            # 新增字段的可选解析
            tenant_info=data.get("tenant_info"),
            connection_uuid=data.get("connection_uuid"),
            tenant_id=data.get("tenant_id"),
            api_key=data.get("api_key"),
            metadata=data.get("metadata"),
        )
