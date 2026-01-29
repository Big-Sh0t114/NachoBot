from dataclasses import dataclass, asdict, field
from typing import List, Optional, Union, Dict, Any


@dataclass
class Seg:
    """消息片段类，用于表示消息的不同部分

    Attributes:
        type: 片段类型，可以是 'text'、'image'、'seglist' 等
        data: 片段的具体内容
            - 对于 text 类型，data 是字符串
            - 对于 image 类型，data 是 base64 字符串
            - 对于 seglist 类型，data 是 Seg 列表
    """

    type: str
    data: Union[str, List["Seg"]]

    @classmethod
    def from_dict(cls, data: Dict) -> "Seg":
        """从字典创建Seg实例"""
        type = data.get("type")
        data = data.get("data")
        if type == "seglist":
            data = [Seg.from_dict(seg) for seg in data]
        return cls(type=type, data=data)

    def to_dict(self) -> Dict:
        """转换为字典格式"""
        result = {"type": self.type}
        if self.type == "seglist":
            result["data"] = [seg.to_dict() for seg in self.data]
        else:
            result["data"] = self.data
        return result


@dataclass
class GroupInfo:
    """群组信息类"""

    platform: str  # 不可缺省
    group_id: str  # 不可缺省
    group_name: Optional[str] = None  # 可缺省，群名称

    def to_dict(self) -> Dict:
        """转换为字典格式"""
        result = {"platform": self.platform, "group_id": self.group_id}
        if self.group_name is not None:
            result["group_name"] = self.group_name
        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "GroupInfo":
        """从字典创建GroupInfo实例

        Args:
            data: 包含必要字段的字典

        Returns:
            GroupInfo: 新的实例
        """
        if data.get("group_id") is None or data.get("platform") is None:
            raise ValueError("GroupInfo requires platform and group_id")
        return cls(
            platform=data["platform"],
            group_id=data["group_id"],
            group_name=data.get("group_name"),
        )


@dataclass
class UserInfo:
    """用户信息类"""

    platform: str  # 不可缺省
    user_id: str  # 不可缺省
    user_nickname: Optional[str] = None  # 可缺省，用户昵称
    user_cardname: Optional[str] = None  # 可缺省，用户群昵称

    def to_dict(self) -> Dict:
        """转换为字典格式"""
        result = {"platform": self.platform, "user_id": self.user_id}
        if self.user_nickname is not None:
            result["user_nickname"] = self.user_nickname
        if self.user_cardname is not None:
            result["user_cardname"] = self.user_cardname
        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "UserInfo":
        """从字典创建UserInfo实例

        Args:
            data: 包含必要字段的字典

        Returns:
            UserInfo: 新的实例
        """
        if data.get("user_id") is None or data.get("platform") is None:
            raise ValueError("UserInfo requires platform and user_id")
        return cls(
            platform=data["platform"],
            user_id=data["user_id"],
            user_nickname=data.get("user_nickname"),
            user_cardname=data.get("user_cardname"),
        )


@dataclass
class InfoBase:
    """信息基类，包含群组和用户信息"""

    group_info: Optional[GroupInfo] = None  # 可缺省
    user_info: Optional[UserInfo] = None  # 可缺省

    def to_dict(self) -> Dict:
        """转换为字典格式"""
        result = {}
        if self.group_info is not None:
            result["group_info"] = self.group_info.to_dict()
        if self.user_info is not None:
            result["user_info"] = self.user_info.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "InfoBase":
        """从字典创建InfoBase实例

        Args:
            data: 包含必要字段的字典

        Returns:
            InfoBase: 新的实例
        """
        group_info = None
        user_info = None

        if data.get("group_info") is not None:
            group_info = GroupInfo.from_dict(data["group_info"])
        if data.get("user_info") is not None:
            user_info = UserInfo.from_dict(data["user_info"])

        return cls(group_info=group_info, user_info=user_info)


@dataclass
class SenderInfo(InfoBase):
    """发送者信息类"""

    pass


@dataclass
class ReceiverInfo(InfoBase):
    """接收者信息类"""

    pass


@dataclass
class FormatInfo:
    """格式信息类"""

    """
    目前nachocore可接受的格式为text,image,emoji
    可发送的格式为text,emoji,reply
    """

    content_format: Optional[List["str"]] = None  # 可缺省
    accept_format: Optional[List["str"]] = None  # 可缺省

    def to_dict(self) -> Dict:
        """转换为字典格式"""
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict) -> "FormatInfo":
        """从字典创建FormatInfo实例
        Args:
            data: 包含必要字段的字典
        Returns:
            FormatInfo: 新的实例
        """
        return cls(
            content_format=data.get("content_format"),
            accept_format=data.get("accept_format"),
        )


@dataclass
class TemplateInfo:
    """模板信息类"""

    template_items: Optional[Dict[str, str]] = None  # 可缺省
    template_name: Optional[Dict[str, str]] = None  # 可缺省
    template_default: bool = True  # 可缺省，默认值为True

    def to_dict(self) -> Dict:
        """转换为字典格式"""
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict) -> "TemplateInfo":
        """从字典创建TemplateInfo实例
        Args:
            data: 包含必要字段的字典
        Returns:
            TemplateInfo: 新的实例
        """
        return cls(
            template_items=data.get("template_items"),
            template_name=data.get("template_name"),
            template_default=data.get("template_default", True),
        )


@dataclass
class MessageDim:
    """消息维度信息类，包含API密钥和平台标识"""

    api_key: str  # 不可缺省，API密钥（包含租户和代理信息）
    platform: str  # 不可缺省，平台标识

    def to_dict(self) -> Dict:
        """转换为字典格式"""
        return {
            "api_key": self.api_key,
            "platform": self.platform
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "MessageDim":
        """从字典创建MessageDim实例

        Args:
            data: 包含必要字段的字典

        Returns:
            MessageDim: 新的实例
        """
        required_fields = ["api_key", "platform"]
        for field in required_fields:
            if field not in data or data[field] is None:
                raise ValueError(f"MessageDim requires {field}")

        return cls(
            api_key=data["api_key"],
            platform=data["platform"],
        )


@dataclass
class BaseMessageInfo:
    """消息信息类"""

    platform: str  # 不可缺省
    message_id: str  # 不可缺省
    time: float  # 不可缺省
    format_info: Optional[FormatInfo] = None  # 可缺省
    template_info: Optional[TemplateInfo] = None  # 可缺省
    additional_config: Optional[dict] = None  # 可缺省
    sender_info: Optional[SenderInfo] = None  # 可缺省
    receiver_info: Optional[ReceiverInfo] = None  # 可缺省

    def to_dict(self) -> Dict:
        """转换为字典格式"""
        result = {
            "platform": self.platform,
            "message_id": self.message_id,
            "time": self.time
        }

        for field, value in [("format_info", self.format_info),
                           ("template_info", self.template_info),
                           ("additional_config", self.additional_config),
                           ("sender_info", self.sender_info),
                           ("receiver_info", self.receiver_info)]:
            if value is not None:
                if isinstance(value, (FormatInfo, TemplateInfo, SenderInfo, ReceiverInfo)):
                    result[field] = value.to_dict()
                else:
                    result[field] = value
        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "BaseMessageInfo":
        """从字典创建BaseMessageInfo实例

        Args:
            data: 包含必要字段的字典

        Returns:
            BaseMessageInfo: 新的实例
        """
        # 验证必需字段
        required_fields = ["platform", "message_id", "time"]
        for field in required_fields:
            if field not in data or data[field] is None:
                raise ValueError(f"BaseMessageInfo requires {field}")

        format_info = None
        template_info = None
        sender_info = None
        receiver_info = None

        if data.get("format_info") is not None:
            format_info = FormatInfo.from_dict(data["format_info"])
        if data.get("template_info") is not None:
            template_info = TemplateInfo.from_dict(data["template_info"])
        if data.get("sender_info") is not None:
            sender_info = SenderInfo.from_dict(data["sender_info"])
        if data.get("receiver_info") is not None:
            receiver_info = ReceiverInfo.from_dict(data["receiver_info"])

        return cls(
            platform=data["platform"],
            message_id=data["message_id"],
            time=data["time"],
            format_info=format_info,
            template_info=template_info,
            additional_config=data.get("additional_config"),
            sender_info=sender_info,
            receiver_info=receiver_info,
        )


@dataclass
class APIMessageBase:
    """API-Server Version消息类，基于双事件循环架构优化"""

    message_info: BaseMessageInfo  # 必需
    message_segment: Seg  # 必需
    message_dim: MessageDim  # 必需

    def to_dict(self) -> Dict:
        """转换为字典格式

        Returns:
            Dict: 包含所有字段的字典，其中：
                - message_info: 转换为字典格式
                - message_segment: 转换为字典格式
                - message_dim: 转换为字典格式
        """
        result = {
            "message_info": self.message_info.to_dict(),
            "message_segment": self.message_segment.to_dict(),
            "message_dim": self.message_dim.to_dict(),
        }
        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "APIMessageBase":
        """从字典创建APIMessageBase实例

        Args:
            data: 包含必要字段的字典

        Returns:
            APIMessageBase: 新的实例
        """
        # 验证必需字段
        required_fields = ["message_info", "message_segment", "message_dim"]
        for field in required_fields:
            if field not in data:
                raise ValueError(f"APIMessageBase requires {field}")

        message_info = BaseMessageInfo.from_dict(data["message_info"])
        message_segment = Seg.from_dict(data["message_segment"])
        message_dim = MessageDim.from_dict(data["message_dim"])

        return cls(
            message_info=message_info,
            message_segment=message_segment,
            message_dim=message_dim,
        )

    def get_api_key(self) -> str:
        """获取API密钥"""
        return self.message_dim.api_key

    def get_platform(self) -> str:
        """获取平台标识"""
        return self.message_dim.platform

    def get_message_platform(self) -> str:
        """获取消息平台标识"""
        return self.message_info.platform

    def get_message_id(self) -> str:
        """获取消息ID"""
        return self.message_info.message_id

    def get_message_time(self) -> float:
        """获取消息时间"""
        return self.message_info.time

    def set_message_dim(
        self,
        api_key: str,
        platform: str,
    ) -> None:
        """设置消息维度信息"""
        self.message_dim.api_key = api_key
        self.message_dim.platform = platform

    def has_sender_info(self) -> bool:
        """检查是否有发送者信息"""
        return self.message_info.sender_info is not None

    def has_receiver_info(self) -> bool:
        """检查是否有接收者信息"""
        return self.message_info.receiver_info is not None