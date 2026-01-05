"""WebSocket业务层配置类 - 统一管理所有回调配置"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Optional, Set
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

from .message import APIMessageBase, BaseMessageInfo, Seg, MessageDim

logger = logging.getLogger(__name__)


class ConfigValidator(ABC):
    """配置验证器基类"""

    @abstractmethod
    def validate(self) -> bool:
        """验证配置是否有效"""
        pass

    @abstractmethod
    def get_missing_fields(self) -> Set[str]:
        """获取缺失的必填字段"""
        pass


@dataclass
class ServerConfig(ConfigValidator):
    """WebSocket服务端配置类"""

    # 基础网络配置
    host: str = "0.0.0.0"
    port: int = 18000
    path: str = "/ws"

    # SSL/TLS配置
    ssl_enabled: bool = False
    ssl_certfile: Optional[str] = None  # SSL证书文件路径
    ssl_keyfile: Optional[str] = None   # SSL私钥文件路径
    ssl_ca_certs: Optional[str] = None  # CA证书文件路径（可选）
    ssl_verify: bool = False             # 是否验证客户端证书

    # 回调函数配置
    on_auth: Optional[Callable[[Dict[str, Any]], bool]] = None
    on_auth_extract_user: Optional[Callable[[Dict[str, Any]], str]] = None
    on_message: Optional[Callable[[APIMessageBase, Dict[str, Any]], None]] = None

    # 自定义消息处理器
    custom_handlers: Dict[str, Callable[[Dict[str, Any]], None]] = field(default_factory=dict)

    # 统计信息配置
    enable_stats: bool = True
    stats_callback: Optional[Callable[[Dict[str, Any]], None]] = None

    # 日志配置
    log_level: str = "INFO"
    enable_connection_log: bool = True
    enable_message_log: bool = True

    def validate(self) -> bool:
        """验证配置是否有效"""
        missing = self.get_missing_fields()
        if missing:
            logger.error(f"服务端配置缺失必填字段: {missing}")
            return False
        return True

    def get_missing_fields(self) -> Set[str]:
        """获取缺失的必填字段"""
        missing = set()

        # 现在所有回调都有默认值，所以没有必填字段
        # 如果需要，可以根据业务需求添加必填字段验证

        return missing

    def get_default_auth_handler(self) -> Callable[[Dict[str, Any]], bool]:
        """获取默认认证处理器"""
        async def default_auth(metadata: Dict[str, Any]) -> bool:
            """默认认证：总是通过，无需API Key验证"""
            logger.info("默认认证通过：无需API Key验证")
            return True
        return default_auth

    def get_default_user_extractor(self) -> Callable[[Dict[str, Any]], str]:
        """获取默认用户标识提取器"""
        async def default_extract_user(metadata: Dict[str, Any]) -> str:
            """默认用户标识提取：所有用户都使用默认用户标识"""
            logger.debug("默认用户标识提取：使用系统默认用户")
            return "sys_default"
        return default_extract_user

    def get_default_message_handler(self) -> Callable[[APIMessageBase, Dict[str, Any]], None]:
        """获取默认消息处理器"""
        async def default_message_handler(message: APIMessageBase, metadata: Dict[str, Any]) -> None:
            """默认消息处理器：记录消息"""
            if self.enable_message_log:
                logger.info(f"收到消息: {message.message_segment.data} "
                           f"from {message.get_api_key()}")
        return default_message_handler

    
    def ensure_defaults(self) -> None:
        """确保所有必填的回调都有默认值"""
        if self.on_auth is None:
            self.on_auth = self.get_default_auth_handler()
            logger.info("使用默认认证处理器")

        if self.on_auth_extract_user is None:
            self.on_auth_extract_user = self.get_default_user_extractor()
            logger.info("使用默认用户标识提取器")

        if self.on_message is None:
            self.on_message = self.get_default_message_handler()
            logger.info("使用默认消息处理器")

    def register_custom_handler(self, message_type: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        """注册自定义消息处理器"""
        if not message_type.startswith("custom_"):
            message_type = f"custom_{message_type}"
        self.custom_handlers[message_type] = handler
        logger.info(f"注册自定义处理器: {message_type}")

    def unregister_custom_handler(self, message_type: str) -> None:
        """注销自定义消息处理器"""
        if not message_type.startswith("custom_"):
            message_type = f"custom_{message_type}"
        self.custom_handlers.pop(message_type, None)
        logger.info(f"注销自定义处理器: {message_type}")


@dataclass
class ClientConfig(ConfigValidator):
    """WebSocket客户端配置类"""

    # 基础连接配置
    url: str
    api_key: str
    platform: str = "default"
    connection_uuid: Optional[str] = None

    # SSL/TLS配置
    ssl_enabled: bool = False                    # 是否启用SSL
    ssl_verify: bool = True                     # 是否验证SSL证书
    ssl_ca_certs: Optional[str] = None         # CA证书文件路径
    ssl_certfile: Optional[str] = None         # 客户端证书文件路径
    ssl_keyfile: Optional[str] = None          # 客户端私钥文件路径
    ssl_check_hostname: bool = True             # 是否检查主机名

    # 重连配置
    auto_reconnect: bool = True
    max_reconnect_attempts: int = 5
    reconnect_delay: float = 2.0
    max_reconnect_delay: float = 10.0

    # 心跳配置
    ping_interval: int = 20
    ping_timeout: int = 10
    close_timeout: int = 10

    # 回调函数配置
    on_message: Optional[Callable[[APIMessageBase, Dict[str, Any]], None]] = None

    # 自定义消息处理器
    custom_handlers: Dict[str, Callable[[Dict[str, Any]], None]] = field(default_factory=dict)

    # 统计信息配置
    enable_stats: bool = True
    stats_callback: Optional[Callable[[Dict[str, Any]], None]] = None

    # 日志配置
    log_level: str = "INFO"
    enable_connection_log: bool = True
    enable_message_log: bool = True

    # HTTP Headers
    headers: Dict[str, str] = field(default_factory=dict)

    def validate(self) -> bool:
        """验证配置是否有效"""
        missing = self.get_missing_fields()
        if missing:
            logger.error(f"客户端配置缺失必填字段: {missing}")
            return False

        # 验证URL格式
        if not (self.url.startswith("ws://") or self.url.startswith("wss://")):
            logger.error("客户端配置错误: URL必须以ws://或wss://开头")
            return False

        return True

    def get_missing_fields(self) -> Set[str]:
        """获取缺失的必填字段"""
        missing = set()

        if not self.url:
            missing.add("url")
        if not self.api_key:
            missing.add("api_key")

        return missing

    
    def get_default_message_handler(self) -> Callable[[APIMessageBase, Dict[str, Any]], None]:
        """获取默认消息处理器"""
        async def default_message_handler(message: APIMessageBase, metadata: Dict[str, Any]) -> None:
            """默认消息处理器：记录消息"""
            if self.enable_message_log:
                logger.info(f"收到消息: {message.message_segment.data}")
        return default_message_handler

    def ensure_defaults(self) -> None:
        """确保所有必填的回调都有默认值"""
        if self.on_message is None:
            self.on_message = self.get_default_message_handler()
            logger.info("使用默认消息处理器")

        # 设置默认headers
        default_headers = {
            "x-apikey": self.api_key,
            "x-platform": self.platform
        }
        for key, value in default_headers.items():
            if key not in self.headers:
                self.headers[key] = value

    def register_custom_handler(self, message_type: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        """注册自定义消息处理器"""
        if not message_type.startswith("custom_"):
            message_type = f"custom_{message_type}"
        self.custom_handlers[message_type] = handler
        logger.info(f"注册自定义处理器: {message_type}")

    def unregister_custom_handler(self, message_type: str) -> None:
        """注销自定义消息处理器"""
        if not message_type.startswith("custom_"):
            message_type = f"custom_{message_type}"
        self.custom_handlers.pop(message_type, None)
        logger.info(f"注销自定义处理器: {message_type}")


@dataclass
class AuthResult:
    """认证结果"""
    success: bool
    user_id: Optional[str] = None
    error_message: Optional[str] = None


class ConfigManager:
    """配置管理器 - 统一管理所有配置的更新和访问"""

    def __init__(self):
        self._server_config: Optional[ServerConfig] = None
        self._client_config: Optional[ClientConfig] = None
        self._config_validators: Dict[str, ConfigValidator] = {}

    def set_server_config(self, config: ServerConfig) -> None:
        """设置服务端配置"""
        if not config.validate():
            raise ValueError("服务端配置验证失败")
        config.ensure_defaults()
        self._server_config = config
        self._config_validators["server"] = config
        logger.info("服务端配置已设置")

    def set_client_config(self, config: ClientConfig) -> None:
        """设置客户端配置"""
        if not config.validate():
            raise ValueError("客户端配置验证失败")
        config.ensure_defaults()
        self._client_config = config
        self._config_validators["client"] = config
        logger.info("客户端配置已设置")

    def get_server_config(self) -> Optional[ServerConfig]:
        """获取服务端配置"""
        return self._server_config

    def get_client_config(self) -> Optional[ClientConfig]:
        """获取客户端配置"""
        return self._client_config

    def update_server_config(self, **kwargs) -> None:
        """更新服务端配置"""
        if self._server_config is None:
            raise ValueError("服务端配置未设置")

        for key, value in kwargs.items():
            if hasattr(self._server_config, key):
                setattr(self._server_config, key, value)
                logger.info(f"服务端配置更新: {key} = {value}")
            else:
                logger.warning(f"无效的服务端配置项: {key}")

        # 重新验证配置
        if not self._server_config.validate():
            raise ValueError("更新后的服务端配置验证失败")

    def update_client_config(self, **kwargs) -> None:
        """更新客户端配置"""
        if self._client_config is None:
            raise ValueError("客户端配置未设置")

        for key, value in kwargs.items():
            if hasattr(self._client_config, key):
                setattr(self._client_config, key, value)
                logger.info(f"客户端配置更新: {key} = {value}")
            else:
                logger.warning(f"无效的客户端配置项: {key}")

        # 重新验证配置
        if not self._client_config.validate():
            raise ValueError("更新后的客户端配置验证失败")

    def validate_all_configs(self) -> bool:
        """验证所有配置"""
        all_valid = True
        for name, config in self._config_validators.items():
            if not config.validate():
                logger.error(f"{name}配置验证失败")
                all_valid = False
            else:
                logger.info(f"{name}配置验证通过")
        return all_valid


# 全局配置管理器实例
config_manager = ConfigManager()


def get_config_manager() -> ConfigManager:
    """获取全局配置管理器"""
    return config_manager


# 便捷函数
def create_server_config(**kwargs) -> ServerConfig:
    """创建服务端配置的便捷函数

    Args:
        host: 监听地址 (默认: "0.0.0.0")
        port: 监听端口 (默认: 18000)
        path: WebSocket路径 (默认: "/ws")
        ssl_enabled: 是否启用SSL (默认: False)
        ssl_certfile: SSL证书文件路径 (ssl_enabled=True时必填)
        ssl_keyfile: SSL私钥文件路径 (ssl_enabled=True时必填)
        ssl_ca_certs: CA证书文件路径 (可选)
        ssl_verify: 是否验证客户端证书 (默认: False)

        # 重要的回调配置
        on_auth: API Key认证回调函数 (签名为: async def(metadata: Dict[str, Any]) -> bool)
        on_auth_extract_user: 用户标识提取回调函数 (签名为: async def(metadata: Dict[str, Any]) -> str)
        on_message: 消息处理回调函数 (签名为: async def(message: APIMessageBase, metadata: Dict[str, Any]) -> None)

    Returns:
        ServerConfig: 服务端配置对象

    Example:
        # 基本配置（使用默认回调）
        config = create_server_config(host="localhost", port=18040)

        # HTTPS服务器
        config = create_server_config(
            host="localhost",
            port=18044,
            ssl_enabled=True,
            ssl_certfile="/path/to/cert.pem",
            ssl_keyfile="/path/to/key.pem"
        )

        # 自定义回调配置
        config = create_server_config(
            host="localhost",
            port=18040,
            on_auth=lambda metadata: metadata.get("api_key") == "valid_key",
            on_auth_extract_user=lambda metadata: metadata["api_key"],
            on_message=lambda message, metadata: print(f"收到消息: {message.message_segment}")
        )
    """
    return ServerConfig(**kwargs)


def create_client_config(url: str, api_key: str, **kwargs) -> ClientConfig:
    """创建客户端配置的便捷函数

    Args:
        url: WebSocket服务器URL
        api_key: API密钥
        platform: 平台标识 (默认: "default")
        ssl_enabled: 是否启用SSL (自动从URL协议检测)
        ssl_verify: 是否验证SSL证书 (默认: True)
        ssl_ca_certs: CA证书文件路径 (可选)
        ssl_certfile: 客户端证书文件路径 (可选)
        ssl_keyfile: 客户端私钥文件路径 (可选)
        ssl_check_hostname: 是否检查主机名 (默认: True)

        # 重要的回调配置
        on_message: 消息处理回调函数 (签名为: async def(message: APIMessageBase, metadata: Dict[str, Any]) -> None)

        ...其他配置参数，包括：
        - auto_reconnect: 是否自动重连 (默认: True)
        - max_reconnect_attempts: 最大重连次数 (默认: 5)
        - reconnect_delay: 重连延迟 (默认: 1.0)
        - ping_interval: ping间隔 (默认: 20)
        - ping_timeout: ping超时 (默认: 10)
        - enable_stats: 是否启用统计 (默认: True)
        - log_level: 日志级别 (默认: "INFO")

    Returns:
        ClientConfig: 客户端配置对象

    Example:
        # 基本配置（使用默认回调）
        config = create_client_config(
            url="ws://localhost:18040/ws",
            api_key="your_api_key",
            platform="test"
        )

        # 自定义消息处理回调
        config = create_client_config(
            url="ws://localhost:18040/ws",
            api_key="your_api_key",
            platform="test",
            on_message=lambda message, metadata: print(f"收到消息: {message.message_segment.data}")
        )

        # HTTPS客户端
        config = create_client_config(
            url="wss://localhost:18044/ws",
            api_key="your_api_key",
            ssl_ca_certs="/path/to/ca.pem",
            on_message=message_handler
        )
    """
    # 自动检测SSL
    if url.startswith("wss://"):
        kwargs["ssl_enabled"] = True

    return ClientConfig(url=url, api_key=api_key, **kwargs)


def create_ssl_server_config(
    host: str = "0.0.0.0",
    port: int = 18044,
    ssl_certfile: str = None,
    ssl_keyfile: str = None,
    **kwargs
) -> ServerConfig:
    """创建SSL服务端配置的便捷函数

    Args:
        host: 监听地址
        port: 监听端口
        ssl_certfile: SSL证书文件路径
        ssl_keyfile: SSL私钥文件路径
        **kwargs: 其他ServerConfig参数，包括重要回调：
            - on_auth: API Key认证回调函数
            - on_auth_extract_user: 用户标识提取回调函数
            - on_message: 消息处理回调函数

    Returns:
        ServerConfig: 配置了SSL的服务端配置

    Example:
        config = create_ssl_server_config(
            host="localhost",
            port=18044,
            ssl_certfile="/path/to/cert.pem",
            ssl_keyfile="/path/to/key.pem",
            on_auth=lambda metadata: metadata.get("api_key") == "valid_key",
            on_auth_extract_user=lambda metadata: metadata["api_key"]
        )
    """
    kwargs.update({
        "host": host,
        "port": port,
        "ssl_enabled": True,
        "ssl_certfile": ssl_certfile,
        "ssl_keyfile": ssl_keyfile
    })
    return create_server_config(**kwargs)


def create_ssl_client_config(
    url: str = None,
    host: str = "localhost",
    port: int = 18044,
    api_key: str = None,
    path: str = "/ws",
    ssl_ca_certs: str = None,
    **kwargs
) -> ClientConfig:
    """创建SSL客户端配置的便捷函数

    Args:
        url: 完整的WebSocket URL (如果提供，会忽略其他参数)
        host: 服务器主机名
        port: 服务器端口
        api_key: API密钥 (必填)
        path: WebSocket路径
        ssl_ca_certs: CA证书文件路径
        **kwargs: 其他ClientConfig参数，包括重要回调：
            - on_message: 消息处理回调函数 (签名为: async def(message: APIMessageBase, metadata: Dict[str, Any]) -> None)
            - platform: 平台标识 (默认: "default")
            - auto_reconnect: 是否自动重连 (默认: True)
            - max_reconnect_attempts: 最大重连次数 (默认: 5)
            - 其他配置参数...

    Returns:
        ClientConfig: 配置了SSL的客户端配置

    Example:
        # 基本SSL客户端配置（使用默认回调）
        config = create_ssl_client_config(
            url="wss://localhost:18044/ws",
            api_key="your_api_key",
            ssl_ca_certs="/path/to/ca.pem"
        )

        # 自定义消息处理回调的SSL客户端
        config = create_ssl_client_config(
            url="wss://localhost:18044/ws",
            api_key="your_api_key",
            ssl_ca_certs="/path/to/ca.pem",
            platform="secure_platform",
            on_message=lambda message, metadata: print(f"安全消息: {message.message_segment.data}")
        )

        # 使用主机名和端口构建
        config = create_ssl_client_config(
            host="secure.example.com",
            port=443,
            api_key="your_api_key",
            path="/websocket",
            ssl_ca_certs="/path/to/ca.pem",
            on_message=message_handler
        )
    """
    if url is None:
        url = f"wss://{host}:{port}{path}"

    kwargs.update({
        "ssl_enabled": True,
        "ssl_ca_certs": ssl_ca_certs
    })

    if api_key is not None:
        return create_client_config(url, api_key, **kwargs)
    else:
        raise ValueError("api_key is required for client configuration")


@dataclass
class ConnectionEntry:
    """连接条目类 - 表示单个连接的配置"""

    name: str
    url: str
    api_key: str
    platform: str = "default"

    # SSL配置
    ssl_enabled: bool = False
    ssl_verify: bool = True
    ssl_ca_certs: Optional[str] = None
    ssl_certfile: Optional[str] = None
    ssl_keyfile: Optional[str] = None
    ssl_check_hostname: bool = True

    # 重连配置
    max_reconnect_attempts: int = 5
    reconnect_delay: float = 2.0

    # 其他配置
    headers: Dict[str, str] = field(default_factory=dict)

    def to_kwargs(self) -> Dict[str, Any]:
        """转换为register_connection所需的kwargs格式"""
        return {
            "ssl_enabled": self.ssl_enabled,
            "ssl_verify": self.ssl_verify,
            "ssl_ca_certs": self.ssl_ca_certs,
            "ssl_certfile": self.ssl_certfile,
            "ssl_keyfile": self.ssl_keyfile,
            "ssl_check_hostname": self.ssl_check_hostname,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "reconnect_delay": self.reconnect_delay,
            "headers": self.headers,
        }


@dataclass
class MultiClientConfig(ConfigValidator):
    """WebSocket 多连接客户端配置类"""

    # 连接配置
    connections: Dict[str, ConnectionEntry] = field(default_factory=dict)

    # 回调函数配置 - 独立于单连接配置
    on_message: Optional[Callable[[APIMessageBase, Dict[str, Any]], None]] = None

    # 自定义消息处理器
    custom_handlers: Dict[str, Callable[[Dict[str, Any]], None]] = field(default_factory=dict)

    # 全局设置
    auto_connect_on_start: bool = False  # 启动时是否自动连接所有注册的连接
    connect_timeout: float = 10.0        # 连接超时时间

    # 统计信息配置
    enable_stats: bool = True
    stats_callback: Optional[Callable[[Dict[str, Any]], None]] = None

    # 日志配置
    log_level: str = "INFO"
    enable_connection_log: bool = True
    enable_message_log: bool = True

    def validate(self) -> bool:
        """验证配置是否有效"""
        if not self.connections:
            logger.warning("多连接客户端配置中没有注册任何连接")
            return True  # 空配置是有效的，但会警告

        # 验证每个连接配置
        for name, conn in self.connections.items():
            if not conn.url:
                logger.error(f"连接 '{name}' 的URL不能为空")
                return False
            if not conn.api_key:
                logger.error(f"连接 '{name}' 的API密钥不能为空")
                return False
            # 验证URL格式
            if not (conn.url.startswith("ws://") or conn.url.startswith("wss://")):
                logger.error(f"连接 '{name}' 的URL格式错误，必须以ws://或wss://开头")
                return False

        return True

    def get_missing_fields(self) -> Set[str]:
        """获取缺失的必填字段"""
        return set()  # 多连接配置没有全局必填字段

    def register_connection(self, name: str, url: str, api_key: str, platform: str = "default", **kwargs) -> None:
        """注册连接配置（一步配置后继续添加）

        Args:
            name: 连接名称
            url: WebSocket URL
            api_key: API密钥
            platform: 平台标识
            **kwargs: 其他连接参数
        """
        connection = ConnectionEntry(
            name=name,
            url=url,
            api_key=api_key,
            platform=platform,
            **kwargs
        )
        self.connections[name] = connection
        logger.info(f"注册连接配置: {name} -> {url} (platform: {platform})")

    def add_connection(self, name: str, url: str, api_key: str, platform: str = "default", **kwargs) -> None:
        """添加连接配置（兼容方法，调用register_connection）"""
        self.register_connection(name, url, api_key, platform, **kwargs)

    def remove_connection(self, name: str) -> bool:
        """移除连接配置"""
        if name in self.connections:
            del self.connections[name]
            logger.info(f"移除连接配置: {name}")
            return True
        else:
            logger.warning(f"连接配置 '{name}' 不存在")
            return False

    def get_connection(self, name: str) -> Optional[ConnectionEntry]:
        """获取连接配置"""
        return self.connections.get(name)

    def list_connections(self) -> Dict[str, ConnectionEntry]:
        """列出所有连接配置"""
        return self.connections.copy()

    def register_custom_handler(self, message_type: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        """注册自定义消息处理器"""
        if not message_type.startswith("custom_"):
            message_type = f"custom_{message_type}"
        self.custom_handlers[message_type] = handler
        logger.info(f"注册自定义处理器: {message_type}")

    def unregister_custom_handler(self, message_type: str) -> None:
        """注销自定义消息处理器"""
        if not message_type.startswith("custom_"):
            message_type = f"custom_{message_type}"
        self.custom_handlers.pop(message_type, None)
        logger.info(f"注销自定义处理器: {message_type}")

    def ensure_defaults(self) -> None:
        """确保所有必填的回调都有默认值"""
        if self.on_message is None:
            self.on_message = self.get_default_message_handler()
            logger.info("使用默认消息处理器")

    def get_default_message_handler(self) -> Callable[[APIMessageBase, Dict[str, Any]], None]:
        """获取默认消息处理器"""
        async def default_message_handler(message: APIMessageBase, metadata: Dict[str, Any]) -> None:
            """默认消息处理器：记录消息"""
            if self.enable_message_log:
                logger.info(f"收到消息: {message.message_segment.data}")
        return default_message_handler

    def register_ssl_connection(self, name: str, url: str, api_key: str, platform: str = "default",
                            ssl_ca_certs: Optional[str] = None, **kwargs) -> None:
        """注册SSL连接配置的便捷方法"""
        if url.startswith("wss://"):
            ssl_kwargs = {
                "ssl_enabled": True,
                "ssl_ca_certs": ssl_ca_certs,
                **kwargs
            }
        else:
            # 自动转换为wss协议
            url = url.replace("ws://", "wss://")
            ssl_kwargs = {
                "ssl_enabled": True,
                "ssl_ca_certs": ssl_ca_certs,
                **kwargs
            }

        self.register_connection(name, url, api_key, platform, **ssl_kwargs)
        logger.info(f"注册SSL连接配置: {name} -> {url}")

    def add_ssl_connection(self, name: str, url: str, api_key: str, platform: str = "default",
                          ssl_ca_certs: Optional[str] = None, **kwargs) -> None:
        """添加SSL连接配置（兼容方法，调用register_ssl_connection）"""
        self.register_ssl_connection(name, url, api_key, platform, ssl_ca_certs, **kwargs)


# 便捷函数
def create_multi_client_config(**kwargs) -> MultiClientConfig:
    """创建多连接客户端配置的便捷函数

    Args:
        auto_connect_on_start: 启动时是否自动连接所有注册的连接 (默认: False)
        connect_timeout: 连接超时时间 (默认: 10.0)
        enable_stats: 是否启用统计信息 (默认: True)
        stats_callback: 统计信息回调函数
        log_level: 日志级别 (默认: "INFO")
        enable_connection_log: 是否启用连接日志 (默认: True)
        enable_message_log: 是否启用消息日志 (默认: True)

        # 重要的回调配置
        on_message: 消息处理回调函数 (签名为: async def(message: APIMessageBase, metadata: Dict[str, Any]) -> None)

    Returns:
        MultiClientConfig: 多连接客户端配置对象

    Example:
        # 创建基本配置（使用默认回调）
        config = create_multi_client_config(
            auto_connect_on_start=True,
            enable_stats=True
        )

        # 自定义消息处理回调
        config = create_multi_client_config(
            auto_connect_on_start=True,
            on_message=lambda message, metadata: print(f"收到消息: {message.message_segment.data}")
        )

        # 添加连接
        config.register_connection("wechat", "ws://localhost:18040/ws", "wechat_key", "wechat")
        config.register_connection("qq", "ws://localhost:18040/ws", "qq_key", "qq")
    """
    config = MultiClientConfig(**kwargs)
    config.ensure_defaults()  # 确保默认回调
    return config


def create_multi_client_config_with_connections(
    connections: Dict[str, Dict[str, Any]],
    **kwargs
) -> MultiClientConfig:
    """创建多连接客户端配置并批量添加连接的便捷函数

    Args:
        connections: 连接配置字典，格式为 {连接名: 连接配置}
            连接配置格式: {
                "url": "ws://localhost:18040/ws",
                "api_key": "api_key",
                "platform": "platform_name",  # 可选
                "ssl_enabled": bool,          # 可选
                "ssl_ca_certs": str,          # 可选
                "max_reconnect_attempts": int, # 可选
                ...其他连接参数
            }
        **kwargs: 其他MultiClientConfig参数，包括：
            - auto_connect_on_start: 是否自动连接 (默认: False)
            - on_message: 消息处理回调函数 (重要)
            - enable_stats: 是否启用统计 (默认: True)
            - 其他配置参数...

    Returns:
        MultiClientConfig: 多连接客户端配置对象

    Example:
        connections = {
            "wechat": {
                "url": "ws://localhost:18040/ws",
                "api_key": "wechat_key",
                "platform": "wechat"
            },
            "qq": {
                "url": "wss://localhost:18044/ws",
                "api_key": "qq_key",
                "platform": "qq",
                "ssl_ca_certs": "/path/to/ca.pem"
            }
        }

        # 基本配置
        config = create_multi_client_config_with_connections(
            connections=connections,
            auto_connect_on_start=True
        )

        # 自定义回调配置
        config = create_multi_client_config_with_connections(
            connections=connections,
            auto_connect_on_start=True,
            on_message=message_handler,
            enable_stats=True
        )
    """
    config = MultiClientConfig(**kwargs)

    for name, conn_config in connections.items():
        url = conn_config.pop("url")
        api_key = conn_config.pop("api_key")
        platform = conn_config.pop("platform", "default")

        config.register_connection(name, url, api_key, platform, **conn_config)

    config.ensure_defaults()  # 确保默认回调
    return config