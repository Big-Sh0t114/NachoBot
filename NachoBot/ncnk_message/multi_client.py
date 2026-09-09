"""WebSocket 多连接客户端 - 专门用于多连接场景，支持连接名称路由"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

from .client_base import WebSocketClientBase
from .client_ws_connection import ConnectionConfig
from .message import APIMessageBase
from .ws_config import MultiClientConfig, ConnectionEntry

logger = logging.getLogger(__name__)


class ConnectionInfo:
    """连接信息类"""

    def __init__(self, name: str, url: str, api_key: str, platform: str, **kwargs):
        self.name = name
        self.url = url
        self.api_key = api_key
        self.platform = platform
        self.kwargs = kwargs
        self.connection_uuid: Optional[str] = None
        self.connected = False
        self.last_error: Optional[str] = None


class WebSocketMultiClient(WebSocketClientBase):
    """WebSocket 多连接客户端 - 专门用于多连接场景

    特点：
    - 支持为每个连接注册名称，用于路由
    - send_message(name, message) - 需要指定连接名称
    - send_custom_message(name, type, payload) - 需要指定连接名称
    - 内部缓存每个连接的参数用于路由
    - 支持连接的生命周期管理
    - 支持MultiClientConfig配置类进行批量配置管理
    """

    def __init__(self, config: Optional[MultiClientConfig] = None):
        # 多连接客户端只接受MultiClientConfig
        self.multi_config = config or MultiClientConfig()
        self.multi_config.ensure_defaults()  # 确保默认回调

        # 调用基类构造函数，不需要default_config
        super().__init__(None)

        # 连接管理
        self.named_connections: Dict[str, ConnectionInfo] = {}
        self.uuid_to_name: Dict[str, str] = {}

        # 连接状态
        self._connected_count = 0

        # 继承基类的统计信息，添加多连接相关统计
        self.stats.update({
            "connections_registered": 0,
            "connections_active": 0,
            "connection_names": [],
        })

    def register_connection(self, name: str, url: str, api_key: str, platform: str = "default", **kwargs) -> bool:
        """注册一个新连接（一步配置后继续添加）

        Args:
            name: 连接名称（用于路由）
            url: WebSocket URL
            api_key: API Key
            platform: 平台标识
            **kwargs: 其他连接参数

        Returns:
            bool: 注册是否成功
        """
        if name in self.named_connections:
            logger.warning(f"连接名称 '{name}' 已存在，将被覆盖")

        # 创建连接信息
        connection_info = ConnectionInfo(name, url, api_key, platform, **kwargs)
        self.named_connections[name] = connection_info
        self.stats["connections_registered"] = len(self.named_connections)

        # 同步更新配置对象
        if self.multi_config:
            # 创建ConnectionEntry并添加到配置中
            conn_entry = ConnectionEntry(
                name=name,
                url=url,
                api_key=api_key,
                platform=platform,
                **kwargs
            )
            self.multi_config.connections[name] = conn_entry
            logger.info(f"同步更新配置对象: 添加连接 {name}")

        logger.info(f"注册连接: {name} -> {url} (api_key: {api_key}, platform: {platform})")
        return True

    def update_connection(self, name: str, **kwargs) -> bool:
        """更新已注册连接的配置

        Args:
            name: 连接名称
            **kwargs: 要更新的连接参数

        Returns:
            bool: 更新是否成功
        """
        if name not in self.named_connections:
            logger.warning(f"连接名称 '{name}' 不存在，无法更新")
            return False

        connection_info = self.named_connections[name]

        # 更新连接信息
        for key, value in kwargs.items():
            if hasattr(connection_info, key):
                setattr(connection_info, key, value)
                logger.info(f"更新连接 {name} 配置: {key} = {value}")

        # 同步更新配置对象
        if self.multi_config and name in self.multi_config.connections:
            conn_entry = self.multi_config.connections[name]
            for key, value in kwargs.items():
                if hasattr(conn_entry, key):
                    setattr(conn_entry, key, value)

        logger.info(f"连接 {name} 配置更新完成")
        return True

    def unregister_connection(self, name: str) -> bool:
        """注销连接

        Args:
            name: 连接名称

        Returns:
            bool: 注销是否成功
        """
        if name not in self.named_connections:
            logger.warning(f"连接名称 '{name}' 不存在")
            return False

        connection_info = self.named_connections[name]

        # 如果连接已建立，先断开
        if connection_info.connection_uuid:
            if connection_info.connected:
                asyncio.create_task(self.disconnect(name))

        # 清理映射
        if connection_info.connection_uuid in self.uuid_to_name:
            del self.uuid_to_name[connection_info.connection_uuid]

        # 移除连接
        del self.named_connections[name]
        self.stats["connections_registered"] = len(self.named_connections)

        # 同步更新配置对象
        if self.multi_config and name in self.multi_config.connections:
            del self.multi_config.connections[name]
            logger.info(f"同步更新配置对象: 移除连接 {name}")

        logger.info(f"注销连接: {name}")
        return True

    async def start(self) -> None:
        """启动客户端"""
        # 如果有配置但还没有应用，先应用配置
        if self.multi_config and not self.named_connections:
            # 批量注册连接
            for name, conn_entry in self.multi_config.connections.items():
                kwargs = conn_entry.to_kwargs()

                # 直接创建连接信息，不调用register_connection避免递归同步
                connection_info = ConnectionInfo(name, conn_entry.url, conn_entry.api_key, conn_entry.platform, **kwargs)
                self.named_connections[name] = connection_info

            logger.info(f"应用MultiClientConfig配置，共注册 {len(self.multi_config.connections)} 个连接")

        await super().start()

        # 如果配置了自动连接，则连接所有注册的连接
        if (self.multi_config and
            self.multi_config.auto_connect_on_start and
            self.named_connections):
            logger.info("启动时自动连接所有注册的连接...")
            await self.connect()

        logger.info("Multi client started")

        """获取活跃连接的信息

        Returns:
            Dict[str, Dict[str, Any]]: 连接名称到连接信息的映射（仅包含已连接的）
        """
        active = {}
        for name, conn_info in self.named_connections.items():
            if conn_info.connected:
                active[name] = {
                    "url": conn_info.url,
                    "api_key": conn_info.api_key,
                    "platform": conn_info.platform,
                    "connection_uuid": conn_info.connection_uuid
                }
        return active

    def register_custom_handler(self, message_type: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        """注册自定义消息处理器"""
        # 注册到配置对象
        self.multi_config.register_custom_handler(message_type, handler)

        # 同时更新基类的处理器
        super().register_custom_handler(message_type, handler)

    def unregister_custom_handler(self, message_type: str) -> None:
        """注销自定义消息处理器"""
        # 从配置对象中注销
        self.multi_config.unregister_custom_handler(message_type)

        # 同时更新基类的处理器
        super().unregister_custom_handler(message_type)

    async def _handle_connect_event(self, event: NetworkEvent) -> None:
        """处理连接事件"""
        connection_uuid = event.connection_uuid

        # 通过UUID找到连接名称
        connection_name = self.uuid_to_name.get(connection_uuid)
        if connection_name:
            connection_info = self.named_connections[connection_name]
            connection_info.connection_uuid = connection_uuid
            connection_info.connected = True
            connection_info.last_error = None
            self._connected_count += 1
            self.stats["successful_connects"] += 1
            self.stats["connections_active"] = self._connected_count

            logger.info(f"连接 '{connection_name}' 已建立 ({connection_uuid})")

    async def _handle_disconnect_event(self, event: NetworkEvent) -> None:
        """处理断连事件"""
        connection_uuid = event.connection_uuid
        connection_name = self.uuid_to_name.get(connection_uuid)

        if connection_name:
            connection_info = self.named_connections[connection_name]
            connection_info.connected = False
            connection_info.last_error = event.error
            self._connected_count -= 1
            self.stats["connections_active"] = max(0, self._connected_count)

            logger.info(f"连接 '{connection_name}' 已断开 ({connection_uuid}): {event.error}")

    async def _handle_standard_message(self, payload: Dict[str, Any]) -> None:
        """处理标准消息"""
        if self.multi_config and self.multi_config.on_message:
            message_data = payload.get("payload", {})
            if message_data:
                from .message import APIMessageBase
                message = APIMessageBase.from_dict(message_data)
                await self.multi_config.on_message(message, payload.get("meta", {}))

    async def connect(self, name: Optional[str] = None) -> Dict[str, bool]:
        """连接指定的连接

        Args:
            name: 连接名称，如果不指定则连接所有注册的连接

        Returns:
            Dict[str, bool]: 连接名称到连接结果的映射
        """
        if not self.running:
            logger.error("Multi client not started")
            return {}

        results = {}

        # 确定要连接的连接列表
        if name:
            connections_to_connect = {name: self.named_connections.get(name)} if name in self.named_connections else {}
        else:
            connections_to_connect = self.named_connections

        # 连接每个连接
        for conn_name, conn_info in connections_to_connect.items():
            if not conn_info:
                results[conn_name] = False
                continue

            # 生成连接UUID
            connection_uuid = f"multi_{conn_name}_{int(time.time() * 1000)}"
            conn_info.connection_uuid = connection_uuid

            # 创建连接配置
            connection_config = ConnectionConfig(
                url=conn_info.url,
                api_key=conn_info.api_key,
                platform=conn_info.platform,
                connection_uuid=connection_uuid,
                headers=conn_info.kwargs.get("headers", {}),
                max_reconnect_attempts=conn_info.kwargs.get("max_reconnect_attempts", 5),
                reconnect_delay=conn_info.kwargs.get("reconnect_delay", 1.0),
                # SSL配置
                ssl_enabled=conn_info.kwargs.get("ssl_enabled", False),
                ssl_verify=conn_info.kwargs.get("ssl_verify", True),
                ssl_ca_certs=conn_info.kwargs.get("ssl_ca_certs"),
                ssl_certfile=conn_info.kwargs.get("ssl_certfile"),
                ssl_keyfile=conn_info.kwargs.get("ssl_keyfile"),
                ssl_check_hostname=conn_info.kwargs.get("ssl_check_hostname", True),
            )

            # 建立UUID到名称的映射
            self.uuid_to_name[connection_uuid] = conn_name

            # 添加并启动连接
            self.stats["connect_attempts"] += 1
            success = await self.network_driver.add_connection(connection_config)
            if success:
                await self.network_driver.connect(connection_uuid)
                results[conn_name] = True
            else:
                results[conn_name] = False
                self.stats["failed_connects"] += 1

        return results

    async def disconnect(self, name: Optional[str] = None) -> Dict[str, bool]:
        """断开指定的连接

        Args:
            name: 连接名称，如果不指定则断开所有连接

        Returns:
            Dict[str, bool]: 连接名称到断开结果的映射
        """
        results = {}

        # 确定要断开的连接列表
        if name:
            connections_to_disconnect = {name: self.named_connections.get(name)} if name in self.named_connections else {}
        else:
            connections_to_disconnect = self.named_connections

        # 断开每个连接
        for conn_name, conn_info in connections_to_disconnect.items():
            if conn_info and conn_info.connection_uuid:
                success = await self.network_driver.disconnect(conn_info.connection_uuid)
                results[conn_name] = success

                if success:
                    # 清理映射
                    if conn_info.connection_uuid in self.uuid_to_name:
                        del self.uuid_to_name[conn_info.connection_uuid]
                    conn_info.connection_uuid = None
            else:
                results[conn_name] = False

        return results

    async def send_message(self, connection_name: str, message: APIMessageBase) -> bool:
        """发送标准消息到指定连接

        Args:
            connection_name: 连接名称
            message: 标准消息对象

        Returns:
            bool: 发送是否成功
        """
        if connection_name not in self.named_connections:
            logger.error(f"连接名称 '{connection_name}' 不存在")
            return False

        conn_info = self.named_connections[connection_name]
        if not conn_info.connected or not conn_info.connection_uuid:
            logger.warning(f"连接 '{connection_name}' 未建立")
            return False

        # 构造消息包，使用该连接的缓存参数
        message_package = {
            "ver": 1,
            "msg_id": f"msg_{int(time.time() * 1000)}",
            "type": "sys_std",
            "meta": {
                "sender_user": conn_info.api_key,      # 使用该连接的API Key
                "platform": conn_info.platform,        # 使用该连接的平台
                "timestamp": time.time(),
            },
            "payload": message.to_dict()
        }

        success = await self.network_driver.send_message(conn_info.connection_uuid, message_package)
        if success:
            self.stats["messages_sent"] += 1
        else:
            logger.error(f"发送消息到连接 '{connection_name}' 失败")

        return success

    async def send_custom_message(self, connection_name: str, message_type: str, payload: Dict[str, Any]) -> bool:
        """发送自定义消息到指定连接

        Args:
            connection_name: 连接名称
            message_type: 自定义消息类型
            payload: 自定义消息载荷

        Returns:
            bool: 发送是否成功
        """
        if connection_name not in self.named_connections:
            logger.error(f"连接名称 '{connection_name}' 不存在")
            return False

        conn_info = self.named_connections[connection_name]
        if not conn_info.connected or not conn_info.connection_uuid:
            logger.warning(f"连接 '{connection_name}' 未建立")
            return False

        # 确保类型前缀
        if not message_type.startswith("custom_"):
            message_type = f"custom_{message_type}"

        # 构造自定义消息包，使用该连接的缓存参数
        message_package = {
            "ver": 1,
            "msg_id": f"custom_{int(time.time() * 1000)}",
            "type": message_type,
            "meta": {
                "sender_user": conn_info.api_key,      # 使用该连接的API Key
                "platform": conn_info.platform,        # 使用该连接的平台
                "timestamp": time.time(),
            },
            "payload": payload
        }

        success = await self.network_driver.send_message(conn_info.connection_uuid, message_package)
        if success:
            self.stats["messages_sent"] += 1
        else:
            logger.error(f"发送自定义消息到连接 '{connection_name}' 失败")

        return success

    def get_connection_info(self, name: str) -> Optional[Dict[str, Any]]:
        """获取指定连接的详细信息

        Args:
            name: 连接名称

        Returns:
            Optional[Dict[str, Any]]: 连接信息，如果不存在返回None
        """
        if name not in self.named_connections:
            return None

        conn_info = self.named_connections[name]
        return {
            "name": conn_info.name,
            "url": conn_info.url,
            "api_key": conn_info.api_key,
            "platform": conn_info.platform,
            "connected": conn_info.connected,
            "connection_uuid": conn_info.connection_uuid,
            "last_error": conn_info.last_error,
            "kwargs": conn_info.kwargs
        }

    def is_connected(self, name: Optional[str] = None) -> bool:
        """检查连接状态

        Args:
            name: 连接名称，如果不指定则检查是否有任何连接

        Returns:
            bool: 连接状态
        """
        if name:
            if name in self.named_connections:
                return self.named_connections[name].connected
            return False
        else:
            return self._connected_count > 0

    def get_last_error(self, name: Optional[str] = None) -> Optional[str]:
        """获取最后的错误信息

        Args:
            name: 连接名称，如果不指定则返回全局错误

        Returns:
            Optional[str]: 错误信息
        """
        if name:
            if name in self.named_connections:
                return self.named_connections[name].last_error
            return None
        else:
            # 返回任意一个连接的错误
            for conn_info in self.named_connections.values():
                if conn_info.last_error:
                    return conn_info.last_error
            return None

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        network_stats = self.network_driver.get_stats()
        return {
            **self.stats,
            "network": network_stats,
            "connections_registered": len(self.named_connections),
            "connections_active": self._connected_count,
            "connection_names": list(self.named_connections.keys())
        }

    async def stop(self) -> None:
        """停止客户端"""
        if not self.running:
            return

        logger.info("Stopping multi WebSocket client...")

        # 断开所有连接
        await self.disconnect()

        # 停止事件分发器
        if self.dispatcher_task:
            self.dispatcher_task.cancel()
            try:
                await self.dispatcher_task
            except asyncio.CancelledError:
                pass

        # 停止网络驱动器
        await self.network_driver.stop()

        self.running = False
        logger.info("Multi client stopped")