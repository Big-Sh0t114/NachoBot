"""WebSocket 单连接客户端 - 专门用于单连接场景"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Dict, Optional

from .client_base import WebSocketClientBase
from .client_ws_connection import EventType, NetworkEvent, ConnectionConfig
from .message import APIMessageBase
from .ws_config import ClientConfig

logger = logging.getLogger(__name__)


class WebSocketClient(WebSocketClientBase):
    """WebSocket 单连接客户端 - 专门用于单连接场景

    特点：
    - 单一连接，无需考虑路由
    - send_message 只需要 APIMessageBase 参数
    - send_custom_message 需要消息类型和载荷
    - 内部自动缓存连接参数用于路由
    """

    def __init__(self, config: ClientConfig):
        # 验证和初始化配置
        if not config.validate():
            raise ValueError("客户端配置验证失败")
        config.ensure_defaults()
        self.config = config

        # 调用基类构造函数
        super().__init__(config)

        # 连接状态
        self.connected = False
        self.last_error: Optional[str] = None

        # 缓存连接参数（用于内部路由）
        self._cached_url = config.url
        self._cached_api_key = config.api_key
        self._cached_platform = config.platform
        self._connection_uuid: Optional[str] = None

        # 继承基类的统计信息，添加连接相关统计
        self.stats.update({
            "connection_uuid": None,
            "cached_connection": {},
        })

    def update_config(self, **kwargs) -> None:
        """更新配置"""
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
                logger.info(f"客户端配置更新: {key} = {value}")
                # 更新缓存的连接参数
                if key == 'url':
                    self._cached_url = value
                elif key == 'api_key':
                    self._cached_api_key = value
                elif key == 'platform':
                    self._cached_platform = value
            else:
                logger.warning(f"无效的配置项: {key}")

        # 重新验证配置
        if not self.config.validate():
            raise ValueError("更新后的配置验证失败")
        self.config.ensure_defaults()

    def register_custom_handler(self, message_type: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        """注册自定义消息处理器"""
        self.config.register_custom_handler(message_type, handler)
        # 同时更新基类的处理器
        super().register_custom_handler(message_type, handler)

    def unregister_custom_handler(self, message_type: str) -> None:
        """注销自定义消息处理器"""
        self.config.unregister_custom_handler(message_type)
        # 同时更新基类的处理器
        super().unregister_custom_handler(message_type)

    async def _handle_connect_event(self, event: NetworkEvent) -> None:
        """处理连接事件"""
        self._connection_uuid = event.connection_uuid
        self.connected = True
        self.last_error = None
        self._connected_count += 1
        self.stats["successful_connects"] += 1

        # 更新缓存
        self.stats["connection_uuid"] = self._connection_uuid
        self.stats["cached_connection"] = {
            "url": self._cached_url,
            "api_key": self._cached_api_key,
            "platform": self._cached_platform,
            "connection_uuid": self._connection_uuid
        }

        logger.info(f"已连接到服务器 ({self._connection_uuid})")

    async def _handle_disconnect_event(self, event: NetworkEvent) -> None:
        """处理断连事件"""
        connection_uuid = event.connection_uuid
        self.connected = False
        self.last_error = event.error
        self._connected_count -= 0  # 单连接只能减到0
        if self._connected_count < 0:
            self._connected_count = 0

        # 清理缓存
        self.stats["connection_uuid"] = None
        self.stats["cached_connection"] = {}

        logger.info(f"与服务器断开连接 ({connection_uuid})")

    async def start(self) -> None:
        """启动客户端"""
        await super().start()
        logger.info("Single client started")

    async def connect(self) -> bool:
        """连接到服务器"""
        if not self.running:
            logger.error("Client not started")
            return False

        if not self._connection_uuid:
            # 生成连接UUID
            self._connection_uuid = f"single_{uuid.uuid4().hex}"
            self.stats["connect_attempts"] += 1

        # 创建连接配置
        connection_config = ConnectionConfig(
            url=self._cached_url,
            api_key=self._cached_api_key,
            platform=self._cached_platform,
            connection_uuid=self._connection_uuid,
            headers=self.config.headers,
            max_reconnect_attempts=self.config.max_reconnect_attempts,
            reconnect_delay=self.config.reconnect_delay,
            ping_interval=self.config.ping_interval,
            ping_timeout=self.config.ping_timeout,
            close_timeout=self.config.close_timeout,
            # SSL配置
            ssl_enabled=self.config.ssl_enabled,
            ssl_verify=self.config.ssl_verify,
            ssl_ca_certs=self.config.ssl_ca_certs,
            ssl_certfile=self.config.ssl_certfile,
            ssl_keyfile=self.config.ssl_keyfile,
            ssl_check_hostname=self.config.ssl_check_hostname,
        )

        # 添加连接
        success = await self.network_driver.add_connection(connection_config)
        if success:
            # 启动连接
            await self.network_driver.connect(self._connection_uuid)

            # 等待连接建立（最多等待10秒）
            logger.info("⏳ 等待连接建立...")
            for i in range(100):  # 等待最多10秒
                await asyncio.sleep(0.1)
                if self.connected:
                    logger.info(f"✅ 连接已建立 ({i * 0.1:.1f}s)")
                    return True
                if not self.running:  # 如果客户端停止了，停止等待
                    break

            logger.warning("⚠️ 连接超时，未收到连接确认")
            self.stats["failed_connects"] += 1
            return False

        self.stats["failed_connects"] += 1
        return False

    async def disconnect(self) -> bool:
        """断开连接"""
        if self._connection_uuid:
            return await self.network_driver.disconnect(self._connection_uuid)
        return False

    async def send_message(self, message: APIMessageBase) -> bool:
        """发送标准消息

        Args:
            message: 标准消息对象

        Returns:
            bool: 发送是否成功
        """
        if not self.connected:
            logger.warning("Not connected, cannot send message")
            return False

        if not self._connection_uuid:
            return False

        # 构造消息包，使用缓存的连接参数作为发送者信息
        message_package = {
            "ver": 1,
            "msg_id": f"msg_{int(time.time() * 1000)}",
            "type": "sys_std",
            "meta": {
                "sender_user": self._cached_api_key,      # 使用缓存的API Key
                "platform": self._cached_platform,        # 使用缓存的平台
                "timestamp": time.time(),
            },
            "payload": message.to_dict()
        }

        success = await self.network_driver.send_message(self._connection_uuid, message_package)
        if success:
            self.stats["messages_sent"] += 1

        return success

    async def send_custom_message(self, message_type: str, payload: Dict[str, Any]) -> bool:
        """发送自定义消息

        Args:
            message_type: 自定义消息类型
            payload: 自定义消息载荷

        Returns:
            bool: 发送是否成功
        """
        if not self.connected:
            logger.warning("Not connected, cannot send custom message")
            return False

        if not self._connection_uuid:
            return False

        # 确保类型前缀
        if not message_type.startswith("custom_"):
            message_type = f"custom_{message_type}"

        # 构造自定义消息包，使用缓存的连接参数
        message_package = {
            "ver": 1,
            "msg_id": f"custom_{int(time.time() * 1000)}",
            "type": message_type,
            "meta": {
                "sender_user": self._cached_api_key,      # 使用缓存的API Key
                "platform": self._cached_platform,        # 使用缓存的平台
                "timestamp": time.time(),
            },
            "payload": payload
        }

        success = await self.network_driver.send_message(self._connection_uuid, message_package)
        if success:
            self.stats["messages_sent"] += 1

        return success

    def get_cached_connection_info(self) -> Dict[str, str]:
        """获取缓存的连接信息"""
        return {
            "url": self._cached_url,
            "api_key": self._cached_api_key,
            "platform": self._cached_platform,
            "connection_uuid": self._connection_uuid or ""
        }

    def get_connection_uuid(self) -> Optional[str]:
        """获取连接UUID"""
        return self._connection_uuid

    def get_last_error(self) -> Optional[str]:
        """获取最后的错误信息"""
        return self.last_error

    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.connected

    async def stop(self) -> None:
        """停止客户端"""
        if not self.running:
            return

        logger.info("Stopping single WebSocket client...")

        # 断开连接
        await self.disconnect()

        # 调用基类的停止方法
        await super().stop()
        logger.info("Single client stopped")

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        network_stats = self.network_driver.get_stats()
        return {
            **self.stats,
            "network": network_stats,
            "connected": self.connected,
            "last_error": self.last_error,
            "connected_count": self._connected_count,
        }