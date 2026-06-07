import asyncio
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Callable, Any, Optional

from src.config.api_ada_configs import ModelInfo, APIProvider
from ..payload_content.message import Message
from ..payload_content.resp_format import RespFormat
from ..payload_content.tool_option import ToolOption, ToolCall


@dataclass
class UsageRecord:
    """
    使用记录类
    """

    model_name: str
    """模型名称"""

    provider_name: str
    """提供商名称"""

    prompt_tokens: int
    """提示token数"""

    completion_tokens: int
    """完成token数"""

    total_tokens: int
    """总token数"""


@dataclass
class APIResponse:
    """
    API响应类
    """

    content: str | None = None
    """响应内容"""

    reasoning_content: str | None = None
    """推理内容"""

    tool_calls: list[ToolCall] | None = None
    """工具调用 [(工具名称, 工具参数), ...]"""

    embedding: list[float] | None = None
    """嵌入向量"""

    usage: UsageRecord | None = None
    """使用情况 (prompt_tokens, completion_tokens, total_tokens)"""

    raw_data: Any = None
    """响应原始数据"""


@dataclass
class EmbeddingRequest:
    """
    嵌入请求封装 — 兼容上游 A_Memorix 的调用约定。
    上游 api_adapter.py 通过 EmbeddingRequest 传递参数，
    而 NachoBot 的 BaseClient.get_embedding 使用位置参数。
    此类作为两者之间的桥梁。
    """

    model_info: ModelInfo
    """模型信息"""

    embedding_input: str
    """嵌入输入文本"""

    extra_params: dict[str, Any] | None = None
    """附加请求参数"""


class BaseClient(ABC):
    """
    基础客户端
    """

    api_provider: APIProvider

    def __init__(self, api_provider: APIProvider):
        self.api_provider = api_provider

    @abstractmethod
    async def get_response(
        self,
        model_info: ModelInfo,
        message_list: list[Message],
        tool_options: list[ToolOption] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        response_format: RespFormat | None = None,
        stream_response_handler: Optional[
            Callable[[Any, asyncio.Event | None], tuple[APIResponse, tuple[int, int, int]]]
        ] = None,
        async_response_parser: Callable[[Any], tuple[APIResponse, tuple[int, int, int]]] | None = None,
        interrupt_flag: asyncio.Event | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> APIResponse:
        """
        获取对话响应
        :param model_info: 模型信息
        :param message_list: 对话体
        :param tool_options: 工具选项（可选，默认为None）
        :param max_tokens: 最大token数（可选，默认为1024）
        :param temperature: 温度（可选，默认为0.7）
        :param response_format: 响应格式（可选，默认为 NotGiven ）
        :param stream_response_handler: 流式响应处理函数（可选）
        :param async_response_parser: 响应解析函数（可选）
        :param interrupt_flag: 中断信号量（可选，默认为None）
        :return: (响应文本, 推理文本, 工具调用, 其他数据)
        """
        raise NotImplementedError("'get_response' method should be overridden in subclasses")

    async def get_embedding(
        self,
        model_info_or_request: ModelInfo | EmbeddingRequest,
        embedding_input: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> APIResponse:
        """
        获取文本嵌入。

        支持两种调用方式：
        1. 传统方式: get_embedding(model_info, embedding_input, extra_params)
        2. A_Memorix 方式: get_embedding(EmbeddingRequest(...))
        """
        if isinstance(model_info_or_request, EmbeddingRequest):
            req = model_info_or_request
            return await self._get_embedding_impl(
                model_info=req.model_info,
                embedding_input=req.embedding_input,
                extra_params=req.extra_params,
            )
        return await self._get_embedding_impl(
            model_info=model_info_or_request,
            embedding_input=embedding_input or "",
            extra_params=extra_params,
        )

    @abstractmethod
    async def _get_embedding_impl(
        self,
        model_info: ModelInfo,
        embedding_input: str,
        extra_params: dict[str, Any] | None = None,
    ) -> APIResponse:
        """
        获取文本嵌入的内部实现（由子类覆写）。
        :param model_info: 模型信息
        :param embedding_input: 嵌入输入文本
        :return: 嵌入响应
        """
        raise NotImplementedError("'_get_embedding_impl' method should be overridden in subclasses")

    @abstractmethod
    async def get_audio_transcriptions(
        self,
        model_info: ModelInfo,
        audio_base64: str,
        extra_params: dict[str, Any] | None = None,
    ) -> APIResponse:
        """
        获取音频转录
        :param model_info: 模型信息
        :param audio_base64: base64编码的音频数据
        :extra_params: 附加的请求参数
        :return: 音频转录响应
        """
        raise NotImplementedError("'get_audio_transcriptions' method should be overridden in subclasses")

    @abstractmethod
    def get_support_image_formats(self) -> list[str]:
        """
        获取支持的图片格式
        :return: 支持的图片格式列表
        """
        raise NotImplementedError("'get_support_image_formats' method should be overridden in subclasses")


class ClientRegistry:
    def __init__(self) -> None:
        self.client_registry: dict[str, type[BaseClient]] = {}
        """APIProvider.type -> BaseClient的映射表"""
        self.client_instance_cache: dict[str, BaseClient] = {}
        """APIProvider.name -> BaseClient的映射表"""

    def register_client_class(self, client_type: str):
        """
        注册API客户端类
        Args:
            client_class: API客户端类
        """

        def decorator(cls: type[BaseClient]) -> type[BaseClient]:
            if not issubclass(cls, BaseClient):
                raise TypeError(f"{cls.__name__} is not a subclass of BaseClient")
            self.client_registry[client_type] = cls
            return cls

        return decorator

    def get_client_class_instance(self, api_provider: APIProvider, force_new=False) -> BaseClient:
        """
        获取注册的API客户端实例
        Args:
            api_provider: APIProvider实例
            force_new: 是否强制创建新实例（用于解决事件循环问题）
        Returns:
            BaseClient: 注册的API客户端实例
        """
        # 如果强制创建新实例，直接创建不使用缓存
        if force_new:
            if client_class := self.client_registry.get(api_provider.client_type):
                return client_class(api_provider)
            else:
                raise KeyError(f"'{api_provider.client_type}' 类型的 Client 未注册")

        # 正常的缓存逻辑
        if api_provider.name not in self.client_instance_cache:
            if client_class := self.client_registry.get(api_provider.client_type):
                self.client_instance_cache[api_provider.name] = client_class(api_provider)
            else:
                raise KeyError(f"'{api_provider.client_type}' 类型的 Client 未注册")
        return self.client_instance_cache[api_provider.name]


client_registry = ClientRegistry()
