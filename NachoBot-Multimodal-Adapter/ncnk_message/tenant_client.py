"""
租户消息客户端 - 支持多租户连接和消息处理
"""

import asyncio
import json
import logging
import uuid
from typing import Optional, Dict, Any, Callable, List, Set
from dataclasses import dataclass
from enum import Enum
import websockets

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    """连接状态枚举"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


@dataclass
class MessageCallback:
    """消息回调配置"""

    callback: Callable
    message_types: List[str]
    tenant_filter: Optional[str] = None
    platform_filter: Optional[str] = None


@dataclass
class PendingMessage:
    """待确认的消息"""

    message_id: str
    message: Dict[str, Any]
    timestamp: float
    retry_count: int = 0
    callback: Optional[Callable] = None


@dataclass
class ClientConfig:
    """客户端配置"""

    tenant_id: str
    platform: str
    server_url: str
    api_key: Optional[str] = None
    connection_pool: Optional[str] = None
    ssl_verify: bool = True
    max_retries: int = 3
    heartbeat_interval: int = 30
    reconnect_interval: int = 5
    message_timeout: float = 10.0


class TenantMessageClient:
    """租户消息客户端"""

    def __init__(
        self,
        config: ClientConfig,
    ):
        self.config = config

        # 连接状态
        self.state = ConnectionState.DISCONNECTED
        self.websocket: Optional[websockets.WebSocketServerProtocol] = None
        self.connection_uuid: Optional[str] = None

        # 消息处理
        self.message_callbacks: List[MessageCallback] = []
        self.pending_messages: Dict[str, PendingMessage] = {}
        self.message_sequence = 0

        # 重连控制
        self._reconnect_attempts = 0
        self._should_reconnect = True

        # 后台任务
        self._background_tasks: Set[asyncio.Task] = set()
        self._running = False

    async def connect(self) -> bool:
        """连接到服务器"""
        if self.state == ConnectionState.CONNECTED:
            return True

        if self.state == ConnectionState.CONNECTING:
            return False

        self.state = ConnectionState.CONNECTING

        try:
            # 构建连接URL
            ws_url = f"{self.config.server_url}/ws/{self.config.tenant_id}/{self.config.platform}"

            # 连接头
            headers = {}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"

            logger.info(f"正在连接到: {ws_url}")

            # 建立WebSocket连接
            ssl_context = (
                self.config.ssl_verify if ws_url.startswith("wss://") else None
            )
            self.websocket = await websockets.connect(
                ws_url,
                ssl=ssl_context,
                ping_interval=self.config.heartbeat_interval,
                ping_timeout=10,
                max_size=10 * 1024 * 1024,  # 10MB
                max_queue=1000,
            )

            # 启动消息处理循环
            self._running = True
            self._should_reconnect = True
            message_task = asyncio.create_task(self._message_loop())
            self._background_tasks.add(message_task)

            # 等待连接确认
            await self._wait_for_connection_confirmation()

            self.state = ConnectionState.CONNECTED
            self._reconnect_attempts = 0
            logger.info(
                f"租户 {self.config.tenant_id} 平台 {self.config.platform} 连接成功"
            )
            return True

        except Exception as e:
            logger.error(f"连接失败: {e}")
            self.state = ConnectionState.DISCONNECTED

            # 如果设置了重连，启动重连逻辑
            if (
                self._should_reconnect
                and self._reconnect_attempts < self.config.max_retries
            ):
                await self._schedule_reconnect()

            return False

    async def _wait_for_connection_confirmation(self):
        """等待连接确认"""
        try:
            async for message in self.websocket:
                data = json.loads(message)
                if data.get("type") == "connection_confirmed":
                    self.connection_uuid = data.get("connection_uuid")
                    logger.info(f"连接确认收到，UUID: {self.connection_uuid}")
                    return
                elif data.get("type") == "error":
                    raise Exception(f"连接错误: {data.get('error_message')}")
        except Exception as e:
            logger.error(f"等待连接确认时出错: {e}")
            raise

    async def _schedule_reconnect(self):
        """调度重连"""
        self._reconnect_attempts += 1
        wait_time = self.config.reconnect_interval * (
            2 ** (self._reconnect_attempts - 1)
        )  # 指数退避

        logger.info(
            f"将在 {wait_time} 秒后进行第 {self._reconnect_attempts} 次重连尝试"
        )
        await asyncio.sleep(wait_time)

        if self._should_reconnect:
            await self.connect()

    async def _message_loop(self):
        """消息处理循环"""
        while self._running:
            try:
                message = await asyncio.wait_for(
                    self.websocket.recv(), timeout=self.config.heartbeat_interval + 5
                )

                data = json.loads(message)
                await self._process_received_message(data)

            except asyncio.TimeoutError:
                # 心跳超时，发送ping
                await self._send_ping()
            except websockets.exceptions.ConnectionClosed:
                logger.info("WebSocket连接已关闭")
                break
            except websockets.exceptions.ConnectionClosedOK:
                logger.info("WebSocket连接正常关闭")
                break
            except Exception as e:
                logger.error(f"消息处理循环出错: {e}")
                break

        self.state = ConnectionState.DISCONNECTED

        # 如果需要重连且不是因为主动断开
        if (
            self._should_reconnect
            and self._reconnect_attempts < self.config.max_retries
        ):
            await self._schedule_reconnect()

    async def _process_received_message(self, data: Dict[str, Any]):
        """处理接收到的消息"""
        message_type = data.get("type")

        if message_type == "heartbeat":
            # 心跳响应
            await self._send_heartbeat_response()
        elif message_type == "pong":
            # Ping响应
            pass
        elif message_type == "message_result":
            # 消息结果确认
            await self._handle_message_result(data)
        elif message_type == "error":
            # 错误消息
            logger.error(f"服务器错误: {data.get('error_message')}")
        else:
            # 触发用户回调
            await self._trigger_callbacks(data)

    async def _trigger_callbacks(self, message: Dict[str, Any]):
        """触发消息回调"""
        for callback_config in self.message_callbacks:
            # 检查消息类型过滤
            if (
                callback_config.message_types
                and message.get("type") not in callback_config.message_types
            ):
                continue

            # 检查租户过滤
            if (
                callback_config.tenant_filter
                and message.get("tenant_id") != callback_config.tenant_filter
            ):
                continue

            # 检查平台过滤
            if (
                callback_config.platform_filter
                and message.get("platform") != callback_config.platform_filter
            ):
                continue

            try:
                result = callback_config.callback(message)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                logger.error(f"回调执行出错: {e}")

    async def send_message(
        self,
        message: Dict[str, Any],
        wait_for_result: bool = True,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """发送消息并等待结果"""
        if not self._is_connected():
            raise ConnectionError("未连接到服务器")

        timeout = timeout or self.config.message_timeout

        # 生成消息ID
        message_id = str(uuid.uuid4())
        self.message_sequence += 1

        # 构建完整消息
        full_message = {
            **message,
            "message_id": message_id,
            "tenant_id": self.config.tenant_id,
            "platform": self.config.platform,
            "connection_uuid": self.connection_uuid,
            "timestamp": asyncio.get_event_loop().time(),
            "sequence": self.message_sequence,
        }

        # 如果需要等待结果，添加到待确认列表
        if wait_for_result:
            pending = PendingMessage(
                message_id=message_id,
                message=full_message,
                timestamp=asyncio.get_event_loop().time(),
            )
            self.pending_messages[message_id] = pending

        try:
            # 发送消息
            await self.websocket.send(json.dumps(full_message))

            if wait_for_result:
                # 等待结果
                return await self._wait_for_message_result(message_id, timeout)

            return {"message_id": message_id, "sent": True}

        except Exception as e:
            # 清理待确认消息
            if wait_for_result and message_id in self.pending_messages:
                del self.pending_messages[message_id]

            logger.error(f"发送消息失败: {e}")
            raise

    async def _wait_for_message_result(
        self, message_id: str, timeout: float
    ) -> Dict[str, Any]:
        """等待消息结果"""
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            if message_id not in self.pending_messages:
                # 消息已被处理
                return {"message_id": message_id, "success": True}

            await asyncio.sleep(0.1)

        # 超时
        if message_id in self.pending_messages:
            del self.pending_messages[message_id]

        raise TimeoutError(f"等待消息 {message_id} 结果超时")

    async def _handle_message_result(self, data: Dict[str, Any]):
        """处理消息结果"""
        message_id = data.get("message_id")
        if message_id and message_id in self.pending_messages:
            pending = self.pending_messages[message_id]

            if pending.callback:
                try:
                    result = pending.callback(data)
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception as e:
                    logger.error(f"消息结果回调出错: {e}")

            # 清理待确认消息
            del self.pending_messages[message_id]

    def register_callback(
        self,
        callback: Callable,
        message_types: List[str],
        tenant_filter: Optional[str] = None,
        platform_filter: Optional[str] = None,
    ):
        """注册消息回调"""
        callback_config = MessageCallback(
            callback=callback,
            message_types=message_types,
            tenant_filter=tenant_filter,
            platform_filter=platform_filter,
        )
        self.message_callbacks.append(callback_config)

    def unregister_callback(self, callback: Callable):
        """取消注册消息回调"""
        self.message_callbacks = [
            cb for cb in self.message_callbacks if cb.callback != callback
        ]

    async def disconnect(self):
        """断开连接"""
        self._should_reconnect = False
        self._running = False

        if self.websocket:
            try:
                await self.websocket.close()
            except Exception as e:
                logger.error(f"关闭WebSocket连接时出错: {e}")
            finally:
                self.websocket = None

        # 取消后台任务
        for task in self._background_tasks:
            task.cancel()

        self._background_tasks.clear()
        self.state = ConnectionState.DISCONNECTED
        logger.info("客户端已断开连接")

    def _is_connected(self) -> bool:
        """检查是否已连接"""
        if self.state != ConnectionState.CONNECTED or self.websocket is None:
            return False

        # 检查websockets库版本兼容性
        # 新版本(15+)使用state属性，旧版本使用closed属性
        if hasattr(self.websocket, "state"):
            # 新版本: state.OPEN (1) 表示连接打开
            from websockets.asyncio.connection import State

            return self.websocket.state == State.OPEN
        elif hasattr(self.websocket, "closed"):
            # 旧版本: closed=False 表示连接打开
            return not self.websocket.closed
        else:
            # 都没有属性时，假设连接正常
            return True

    async def _send_ping(self):
        """发送ping"""
        if self._is_connected():
            try:
                await self.websocket.send(json.dumps({"type": "ping"}))
            except Exception as e:
                logger.error(f"发送ping失败: {e}")

    async def _send_heartbeat_response(self):
        """发送心跳响应"""
        if self._is_connected():
            try:
                await self.websocket.send(
                    json.dumps(
                        {
                            "type": "heartbeat_response",
                            "timestamp": asyncio.get_event_loop().time(),
                        }
                    )
                )
            except Exception as e:
                logger.error(f"发送心跳响应失败: {e}")

    async def get_connection_info(self) -> Dict[str, Any]:
        """获取连接信息"""
        return {
            "state": self.state.value,
            "tenant_id": self.config.tenant_id,
            "platform": self.config.platform,
            "connection_uuid": self.connection_uuid,
            "server_url": self.config.server_url,
            "is_connected": self._is_connected(),
            "message_callbacks_count": len(self.message_callbacks),
            "pending_messages_count": len(self.pending_messages),
            "message_sequence": self.message_sequence,
            "reconnect_attempts": self._reconnect_attempts,
        }

    async def send_message_with_retry(
        self,
        message: Dict[str, Any],
        max_retries: int = 3,
        wait_for_result: bool = True,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """发送消息，支持重试"""
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                if not self._is_connected():
                    await self.connect()

                return await self.send_message(message, wait_for_result, timeout)

            except Exception as e:
                last_exception = e
                logger.warning(
                    f"发送消息失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}"
                )

                if attempt < max_retries:
                    wait_time = 0.5 * (2**attempt)  # 指数退避
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    await asyncio.sleep(wait_time)

        raise last_exception


# 便捷函数
async def create_tenant_client(
    tenant_id: str,
    platform: str,
    server_url: str,
    api_key: Optional[str] = None,
    **kwargs,
) -> TenantMessageClient:
    """创建租户客户端并连接"""
    config = ClientConfig(
        tenant_id=tenant_id,
        platform=platform,
        server_url=server_url,
        api_key=api_key,
        **kwargs,
    )

    client = TenantMessageClient(config)

    if await client.connect():
        return client
    else:
        raise ConnectionError(f"无法连接到服务器: {server_url}")


# 使用示例
async def example_usage():
    """使用示例"""
    try:
        # 创建客户端
        client = await create_tenant_client(
            tenant_id="tenant1",
            platform="qq",
            server_url="wss://message-server.example.com:8091",
            api_key="your_api_key",
        )

        # 注册消息回调
        def handle_chat_message(message):
            print(f"收到聊天消息: {message.get('type')} - {message.get('content')}")

        client.register_callback(
            callback=handle_chat_message,
            message_types=["chat_message", "group_message"],
        )

        # 发送消息
        result = await client.send_message(
            {"type": "chat_message", "content": "Hello from client!"}
        )

        print(f"消息发送结果: {result}")

        # 获取连接信息
        info = await client.get_connection_info()
        print(f"连接信息: {info}")

        # 保持连接
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass

    finally:
        # 断开连接
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(example_usage())
