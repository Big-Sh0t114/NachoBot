from abc import ABC, abstractmethod
from typing import Tuple, Optional, Dict, List, TYPE_CHECKING

from src.common.logger import get_logger
from src.common.data_models.message_data_model import ReplyContentType, ReplySetModel, ReplyContent, ForwardNode
from src.plugin_system.apis import send_api
from .component_types import MaiMessages, EventType, EventHandlerInfo, ComponentType, CustomEventHandlerResult

logger = get_logger("base_event_handler")

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages


class BaseEventHandler(ABC):
    """事件处理器基类

    所有事件处理器都应该继承这个基类，提供事件处理的基本接口
    """

    event_type: EventType | str = EventType.UNKNOWN
    """事件类型，默认为未知"""
    handler_name: str = ""
    """处理器名称"""
    handler_description: str = ""
    """处理器描述"""
    weight: int = 0
    """处理器权重，越大权重越高"""
    intercept_message: bool = False
    """是否拦截消息，默认为否"""

    def __init__(self):
        self.log_prefix = "[EventHandler]"
        self.plugin_name = ""
        """对应插件名"""
        self.plugin_config: Optional[Dict] = None
        """插件配置字典"""
        if self.event_type == EventType.UNKNOWN:
            raise NotImplementedError("事件处理器必须指定 event_type")

    @abstractmethod
    async def execute(
        self, message: MaiMessages | None
    ) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[MaiMessages]]:
        """执行事件处理的抽象方法，子类必须实现
        Args:
            message (MaiMessages | None): 事件消息对象，当你注册的事件为ON_START和ON_STOP时message为None
        Returns:
            Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[MaiMessages]]: (是否执行成功, 是否需要继续处理, 可选的返回消息, 可选的自定义结果，可选的修改后消息)
        """
        raise NotImplementedError("子类必须实现 execute 方法")

    @classmethod
    def get_handler_info(cls) -> "EventHandlerInfo":
        """获取事件处理器的信息"""
        # 从类属性读取名称，如果没有定义则使用类名自动生成S
        name: str = getattr(cls, "handler_name", cls.__name__.lower().replace("handler", ""))
        if "." in name:
            logger.error(f"事件处理器名称 '{name}' 包含非法字符 '.'，请使用下划线替代")
            raise ValueError(f"事件处理器名称 '{name}' 包含非法字符 '.'，请使用下划线替代")
        return EventHandlerInfo(
            name=name,
            component_type=ComponentType.EVENT_HANDLER,
            description=getattr(cls, "handler_description", "events处理器"),
            event_type=cls.event_type,
            weight=cls.weight,
            intercept_message=cls.intercept_message,
        )

    def set_plugin_config(self, plugin_config: Dict) -> None:
        """设置插件配置

        Args:
            plugin_config (dict): 插件配置字典
        """
        self.plugin_config = plugin_config

    def set_plugin_name(self, plugin_name: str) -> None:
        """设置插件名称

        Args:
            plugin_name (str): 插件名称
        """
        self.plugin_name = plugin_name

    def get_config(self, key: str, default=None):
        """获取插件配置值，支持嵌套键访问

        Args:
            key: 配置键名，支持嵌套访问如 "section.subsection.key"
            default: 默认值

        Returns:
            Any: 配置值或默认值
        """
        if not self.plugin_config:
            return default

        # 支持嵌套键访问
        keys = key.split(".")
        current = self.plugin_config

        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default

        return current

    async def send_text(
        self,
        stream_id: str,
        text: str,
        set_reply: bool = False,
        reply_message: Optional["DatabaseMessages"] = None,
        typing: bool = False,
        storage_message: bool = True,
    ) -> bool:
        """发送文本消息

        Args:
            stream_id: 聊天ID
            text: 文本内容
            set_reply: 是否作为回复发送
            reply_message: 回复的消息对象（当set_reply为True时必填）
            typing: 是否计算输入时间
            storage_message: 是否存储消息到数据库

        Returns:
            bool: 是否发送成功
        """
        if not stream_id:
            logger.error(f"{self.log_prefix} 缺少聊天ID")
            return False
        return await send_api.text_to_stream(
            text=text,
            stream_id=stream_id,
            set_reply=set_reply,
            reply_message=reply_message,
            typing=typing,
            storage_message=storage_message,
        )

    async def send_emoji(
        self,
        stream_id: str,
        emoji_base64: str,
        set_reply: bool = False,
        reply_message: Optional["DatabaseMessages"] = None,
        storage_message: bool = True,
    ) -> bool:
        """发送表情消息

        Args:
            emoji_base64: 表情的Base64编码
            stream_id: 聊天ID
            set_reply: 是否作为回复发送
            reply_message: 回复的消息对象（当set_reply为True时必填）
            storage_message: 是否存储消息到数据库

        Returns:
            bool: 是否发送成功
        """
        if not stream_id:
            logger.error(f"{self.log_prefix} 缺少聊天ID")
            return False
        return await send_api.emoji_to_stream(
            emoji_base64=emoji_base64,
            stream_id=stream_id,
            set_reply=set_reply,
            reply_message=reply_message,
            storage_message=storage_message,
        )

    async def send_image(
        self,
        stream_id: str,
        image_base64: str,
        set_reply: bool = False,
        reply_message: Optional["DatabaseMessages"] = None,
        storage_message: bool = True,
    ) -> bool:
        """发送图片消息

        Args:
            image_base64: 图片的Base64编码
            stream_id: 聊天ID
            set_reply: 是否作为回复发送
            reply_message: 回复的消息对象（当set_reply为True时必填）
            storage_message: 是否存储消息到数据库

        Returns:
            bool: 是否发送成功
        """
        if not stream_id:
            logger.error(f"{self.log_prefix} 缺少聊天ID")
            return False
        return await send_api.image_to_stream(
            image_base64=image_base64,
            stream_id=stream_id,
            set_reply=set_reply,
            reply_message=reply_message,
            storage_message=storage_message,
        )

    async def send_voice(
        self,
        stream_id: str,
        audio_base64: str,
    ) -> bool:
        """发送语音消息
        Args:
            stream_id: 聊天ID
            audio_base64: 语音的Base64编码
        Returns:
            bool: 是否发送成功
        """
        if not stream_id:
            logger.error(f"{self.log_prefix} 缺少聊天ID")
            return False
        reply_set = ReplySetModel()
        reply_set.add_voice_content(audio_base64)
        return await send_api.custom_reply_set_to_stream(
            reply_set=reply_set,
            stream_id=stream_id,
            storage_message=False,
        )

    async def send_command(
        self,
        stream_id: str,
        command_name: str,
        command_args: Optional[dict] = None,
        display_message: str = "",
        storage_message: bool = True,
    ) -> bool:
        """发送命令消息

        Args:
            stream_id: 流ID
            command_name: 命令名称
            command_args: 命令参数字典
            display_message: 显示消息
            storage_message: 是否存储消息到数据库

        Returns:
            bool: 是否发送成功
        """
        if not stream_id:
            logger.error(f"{self.log_prefix} 缺少聊天ID")
            return False

        # 构造命令数据
        command_data = {"name": command_name, "args": command_args or {}}

        return await send_api.command_to_stream(
            command=command_data,
            stream_id=stream_id,
            storage_message=storage_message,
            display_message=display_message,
        )

    async def send_custom(
        self,
        stream_id: str,
        message_type: str,
        content: str | Dict,
        typing: bool = False,
        set_reply: bool = False,
        reply_message: Optional["DatabaseMessages"] = None,
        storage_message: bool = True,
    ) -> bool:
        """发送自定义消息

        Args:
            stream_id: 聊天ID
            message_type: 消息类型
            content: 消息内容，可以是字符串或字典
            typing: 是否显示正在输入状态
            set_reply: 是否作为回复发送
            reply_message: 回复的消息对象（当set_reply为True时必填）
            storage_message: 是否存储消息到数据库

        Returns:
            bool: 是否发送成功
        """
        if not stream_id:
            logger.error(f"{self.log_prefix} 缺少聊天ID")
            return False
        return await send_api.custom_to_stream(
            message_type=message_type,
            content=content,
            stream_id=stream_id,
            typing=typing,
            set_reply=set_reply,
            reply_message=reply_message,
            storage_message=storage_message,
        )

    async def send_hybrid(
        self,
        stream_id: str,
        message_tuple_list: List[Tuple[ReplyContentType | str, str]],
        typing: bool = False,
        set_reply: bool = False,
        reply_message: Optional["DatabaseMessages"] = None,
        storage_message: bool = True,
    ) -> bool:
        """
        发送混合类型消息

        Args:
            stream_id: 流ID
            message_tuple_list: 包含消息类型和内容的元组列表，格式为 [(内容类型, 内容), ...]
            typing: 是否计算打字时间
            set_reply: 是否作为回复发送
            reply_message: 回复的消息对象
            storage_message: 是否存储消息到数据库
        """
        if not stream_id:
            logger.error(f"{self.log_prefix} 缺少聊天ID")
            return False
        reply_set = ReplySetModel()
        reply_set.add_hybrid_content_by_raw(message_tuple_list)
        return await send_api.custom_reply_set_to_stream(
            reply_set=reply_set,
            stream_id=stream_id,
            typing=typing,
            set_reply=set_reply,
            reply_message=reply_message,
            storage_message=storage_message,
        )

    async def send_forward(
        self,
        stream_id: str,
        messages_list: List[Tuple[str, str, List[Tuple[ReplyContentType | str, str]]] | str],
        storage_message: bool = True,
    ) -> bool:
        """转发消息

        Args:
            stream_id: 聊天ID
            messages_list: 包含消息信息的列表，当传入自行生成的数据时，元素格式为 (sender_id, nickname, 消息体)；当传入消息ID时，元素格式为 "message_id"
            其中消息体的格式为 [(内容类型, 内容), ...]
            任意长度的消息都需要使用列表的形式传入
            storage_message: 是否存储消息到数据库

        Returns:
            bool: 是否发送成功
        """
        if not stream_id:
            logger.error(f"{self.log_prefix} 缺少聊天ID")
            return False
        reply_set = ReplySetModel()
        forward_message_nodes: List[ForwardNode] = []
        for message in messages_list:
            if isinstance(message, str):
                forward_message_node = ForwardNode.construct_as_id_reference(message)
            elif isinstance(message, Tuple) and len(message) == 3:
                sender_id, nickname, content_list = message
                single_node_content_list: List[ReplyContent] = []
                for node_content_type, node_content in content_list:
                    reply_node_content = ReplyContent(content_type=node_content_type, content=node_content)
                    single_node_content_list.append(reply_node_content)
                forward_message_node = ForwardNode.construct_as_created_node(
                    user_id=sender_id, user_nickname=nickname, content=single_node_content_list
                )
            else:
                logger.warning(f"{self.log_prefix} 转发消息时遇到无效的消息格式: {message}")
                continue
            forward_message_nodes.append(forward_message_node)
        reply_set.add_forward_content(forward_message_nodes)
        return await send_api.custom_reply_set_to_stream(
            reply_set=reply_set,
            stream_id=stream_id,
            storage_message=storage_message,
            set_reply=False,
            reply_message=None,
        )
