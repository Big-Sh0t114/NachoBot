"""
线程化租户消息客户端 - 为每个连接单独开线程，彻底杜绝协程堵塞
"""

import asyncio
import json
import logging
import uuid
import threading
import time
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass
from enum import Enum
import websockets
from queue import Queue, Empty
import weakref
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    """连接状态枚举"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


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
    future: Optional[asyncio.Future] = None


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
    thread_pool_size: int = 10


class ConnectionThreadManager:
    """连接线程管理器 - 管理客户端连接的专用线程"""

    def __init__(self):
        self.connection_thread: Optional[threading.Thread] = None
        self.connection_loop_ref: Optional[weakref.ref] = None
        self.executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="ClientWorker")

    def start_connection_thread(self, client_ref: weakref.ref) -> bool:
        """启动连接管理线程"""
        try:
            thread = threading.Thread(
                target=self._connection_worker,
                args=(client_ref,),
                name=f"WS-Client-{client_ref().config.tenant_id}-{client_ref().config.platform}",
                daemon=True
            )

            thread.start()
            self.connection_thread = thread

            logger.info(f"客户端连接线程已启动 (线程ID: {thread.ident})")
            return True

        except Exception as e:
            logger.error(f"启动客户端连接线程失败: {e}")
            return False

    def _connection_worker(self, client_ref: weakref.ref):
        """客户端连接工作线程"""
        client = client_ref()
        if not client:
            return

        try:
            # 在线程中创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 保存循环引用
            self.connection_loop_ref = weakref.ref(loop)

            # 运行连接管理逻辑
            loop.run_until_complete(self._manage_connection_loop(client))

        except Exception as e:
            logger.error(f"客户端连接线程异常: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

            self.connection_thread = None
            self.connection_loop_ref = None
            logger.info("客户端连接线程已退出")

    async def _manage_connection_loop(self, client):
        """管理连接的主循环"""
        try:
            while client._should_run:
                # 连接状态管理
                if not client._is_connected():
                    if client._should_reconnect:
                        await self._attempt_reconnection(client)
                    else:
                        await asyncio.sleep(1)
                        continue

                # 处理消息队列
                await self._process_outgoing_messages(client)

                # 处理待确认消息超时
                await self._check_pending_timeouts(client)

                # 短暂休眠
                await asyncio.sleep(0.01)

        except Exception as e:
            logger.error(f"连接管理循环异常: {e}")

    async def _attempt_reconnection(self, client):
        """尝试重连"""
        if client._reconnect_attempts >= client.config.max_retries:
            logger.error(f"重连次数已达上限: {client._reconnect_attempts}")
            client._should_reconnect = False
            return

        client._reconnect_attempts += 1
        client.state = ConnectionState.RECONNECTING

        wait_time = client.config.reconnect_interval * (2 ** (client._reconnect_attempts - 1))
        logger.info(f"将在 {wait_time} 秒后进行第 {client._reconnect_attempts} 次重连尝试")

        await asyncio.sleep(wait_time)

        if client._should_reconnect:
            await self._establish_connection(client)

    async def _establish_connection(self, client):
        """建立WebSocket连接"""
        try:
            client.state = ConnectionState.CONNECTING

            ws_url = f"{client.config.server_url}/ws/{client.config.tenant_id}/{client.config.platform}"
            headers = {}
            if client.config.api_key:
                headers["Authorization"] = f"Bearer {client.config.api_key}"

            logger.info(f"正在连接到: {ws_url}")

            # 根据协议设置SSL
            ssl_context = client.config.ssl_verify if ws_url.startswith("wss://") else None

            # 建立WebSocket连接
            client.websocket = await websockets.connect(
                ws_url,
                ssl=ssl_context,
                ping_interval=client.config.heartbeat_interval,
                ping_timeout=10,
                max_size=10 * 1024 * 1024,  # 10MB
                max_queue=1000,
            )

            # 启动消息接收循环
            await self._start_message_receiver(client)

            client.state = ConnectionState.CONNECTED
            client._reconnect_attempts = 0
            logger.info(f"连接成功: {client.config.tenant_id}/{client.config.platform}")

        except Exception as e:
            logger.error(f"连接失败: {e}")
            client.state = ConnectionState.ERROR
            raise

    async def _start_message_receiver(self, client):
        """启动消息接收循环"""
        async def message_receiver():
            try:
                async for message in client.websocket:
                    try:
                        data = json.loads(message)
                        await client._process_received_message(data)
                    except json.JSONDecodeError:
                        logger.error("收到无效JSON消息")
                    except Exception as e:
                        logger.error(f"消息处理异常: {e}")

            except websockets.exceptions.ConnectionClosed:
                logger.info("WebSocket连接已关闭")
            except Exception as e:
                logger.error(f"消息接收循环异常: {e}")
            finally:
                client.state = ConnectionState.DISCONNECTED

        # 在线程池中运行接收循环
        asyncio.create_task(message_receiver())

    async def _process_outgoing_messages(self, client):
        """处理待发送消息队列"""
        try:
            while not client.outgoing_messages.empty():
                try:
                    message_data = client.outgoing_messages.get_nowait()

                    # 发送消息
                    await client.websocket.send(json.dumps(message_data))
                    client.message_sequence += 1

                except Empty:
                    break
                except Exception as e:
                    logger.error(f"发送消息失败: {e}")

        except Exception as e:
            logger.error(f"处理消息队列异常: {e}")

    async def _check_pending_timeouts(self, client):
        """检查待确认消息超时"""
        current_time = time.time()
        timeout_messages = []

        for message_id, pending in client.pending_messages.items():
            if current_time - pending.timestamp > client.config.message_timeout:
                timeout_messages.append(message_id)

        for message_id in timeout_messages:
            pending = client.pending_messages.pop(message_id, None)
            if pending and pending.future:
                pending.future.set_exception(TimeoutError(f"消息 {message_id} 确认超时"))

    def stop(self):
        """停止连接线程"""
        if self.connection_thread and self.connection_thread.is_alive():
            # 获取事件循环并停止
            if self.connection_loop_ref:
                loop = self.connection_loop_ref()
                if loop and not loop.is_closed():
                    # 在循环中调用停止函数
                    asyncio.run_coroutine_threadsafe(
                        self._stop_connection_loop(), loop
                    )

            # 等待线程结束
            self.connection_thread.join(timeout=5.0)

        self.executor.shutdown(wait=True)

    async def _stop_connection_loop(self):
        """停止连接循环"""
        pass  # 会被重写


class TenantMessageClientThreaded:
    """线程化租户消息客户端"""

    def __init__(self, config: ClientConfig):
        self.config = config
        self.websocket = None
        self.connection_uuid: Optional[str] = None
        self.message_sequence = 0
        self.state = ConnectionState.DISCONNECTED

        # 消息管理
        self.outgoing_messages: Queue = Queue()
        self.pending_messages: Dict[str, PendingMessage] = {}
        self.message_callbacks: List[MessageCallback] = []

        # 连接管理
        self._should_run = True
        self._should_reconnect = True
        self._reconnect_attempts = 0

        # 线程管理
        self.thread_manager = ConnectionThreadManager()

    async def connect(self) -> bool:
        """连接到服务器"""
        self._should_run = True
        self._should_reconnect = True
        self._reconnect_attempts = 0

        # 启动连接管理线程
        if self.thread_manager.start_connection_thread(weakref.ref(self)):
            # 等待连接建立
            for _ in range(50):  # 最多等待5秒
                if self.state == ConnectionState.CONNECTED:
                    return True
                await asyncio.sleep(0.1)

            return False
        else:
            return False

    async def disconnect(self):
        """断开连接"""
        self._should_run = False
        self._should_reconnect = False

        # 关闭WebSocket连接
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception as e:
                logger.error(f"关闭WebSocket连接失败: {e}")

        # 停止连接线程
        self.thread_manager.stop()

        self.state = ConnectionState.DISCONNECTED
        logger.info("客户端已断开连接")

    async def send_message(
        self,
        message: Dict[str, Any],
        wait_for_result: bool = True,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """发送消息"""
        if not self._is_connected():
            raise ConnectionError("未连接到服务器")

        timeout = timeout or self.config.message_timeout
        message_id = str(uuid.uuid4())

        # 构建完整消息
        full_message = {
            **message,
            "message_id": message_id,
            "tenant_id": self.config.tenant_id,
            "platform": self.config.platform,
            "connection_uuid": self.connection_uuid,
            "timestamp": time.time(),
            "sequence": self.message_sequence + 1,
        }

        # 如果需要等待结果，创建Future
        future = None
        if wait_for_result:
            future = asyncio.Future()
            pending = PendingMessage(
                message_id=message_id,
                message=full_message,
                timestamp=time.time(),
                future=future
            )
            self.pending_messages[message_id] = pending

        try:
            # 将消息放入发送队列
            self.outgoing_messages.put(full_message)

            if wait_for_result and future:
                # 等待结果
                try:
                    result = await asyncio.wait_for(future, timeout=timeout)
                    return result
                except asyncio.TimeoutError:
                    # 清理超时的消息
                    self.pending_messages.pop(message_id, None)
                    raise TimeoutError(f"消息 {message_id} 确认超时")

            return {"message_id": message_id, "status": "sent"}

        except Exception as e:
            # 清理待确认消息
            if message_id in self.pending_messages:
                del self.pending_messages[message_id]
            raise e

    async def _process_received_message(self, data: Dict[str, Any]):
        """处理接收到的消息"""
        message_type = data.get("type")

        # 处理连接确认
        if message_type == "connection_confirmation":
            self.connection_uuid = data.get("connection_uuid")
            logger.info(f"连接确认收到，UUID: {self.connection_uuid}")

        # 处理消息结果确认
        elif message_type == "message_result":
            message_id = data.get("message_id")
            pending = self.pending_messages.get(message_id)
            if pending and pending.future:
                if data.get("success", False):
                    pending.future.set_result(data)
                else:
                    pending.future.set_exception(Exception(data.get("error_message", "消息处理失败")))

                # 清理待确认消息
                del self.pending_messages[message_id]

        # 处理心跳
        elif message_type == "heartbeat":
            await self._send_heartbeat_response()

        # 处理pong
        elif message_type == "pong":
            logger.debug("收到pong响应")

        else:
            # 触发用户回调
            await self._trigger_callbacks(data)

    async def _send_heartbeat_response(self):
        """发送心跳响应"""
        try:
            response = {
                "type": "heartbeat_response",
                "timestamp": time.time(),
                "connection_uuid": self.connection_uuid
            }
            self.outgoing_messages.put(response)
        except Exception as e:
            logger.error(f"发送心跳响应失败: {e}")

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
                    # 在线程池中运行异步回调
                    asyncio.create_task(result)
            except Exception as e:
                logger.error(f"回调执行出错: {e}")

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

    def _is_connected(self) -> bool:
        """检查是否已连接"""
        return (
            self.state == ConnectionState.CONNECTED
            and self.websocket is not None
            and hasattr(self.websocket, 'state')
            and self.websocket.state.name == 'OPEN'
        )

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
            "threaded": True
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
                    # 等待重连
                    for _ in range(50):  # 最多等待5秒
                        if self._is_connected():
                            break
                        await asyncio.sleep(0.1)

                return await self.send_message(message, wait_for_result, timeout)

            except Exception as e:
                last_exception = e
                logger.warning(f"发送消息失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")

                if attempt < max_retries:
                    wait_time = 0.5 * (2 ** attempt)  # 指数退避
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    await asyncio.sleep(wait_time)

        raise last_exception


# 便捷函数
async def create_threaded_tenant_client(
    tenant_id: str,
    platform: str,
    server_url: str,
    api_key: Optional[str] = None,
    **kwargs,
) -> TenantMessageClientThreaded:
    """创建线程化租户客户端并连接"""
    config = ClientConfig(
        tenant_id=tenant_id,
        platform=platform,
        server_url=server_url,
        api_key=api_key,
        **kwargs,
    )

    client = TenantMessageClientThreaded(config)

    if await client.connect():
        return client
    else:
        raise ConnectionError(f"无法连接到服务器: {server_url}")


if __name__ == "__main__":
    # 使用示例
    async def example_usage():
        try:
            # 创建客户端
            client = await create_threaded_tenant_client(
                tenant_id="test_tenant",
                platform="qq",
                server_url="ws://localhost:8091",
                api_key="test_api_key",
            )

            # 注册消息回调
            def handle_chat_message(message):
                print(f"收到聊天消息: {message.get('type')} - {message.get('content')}")

            client.register_callback(
                callback=handle_chat_message,
                message_types=["chat_message", "chat_response"],
            )

            # 发送消息
            result = await client.send_message(
                {"type": "chat_message", "content": "Hello from threaded client!"}
            )

            print(f"消息发送结果: {result}")

            # 获取连接信息
            info = await client.get_connection_info()
            print(f"连接信息: {info}")

            # 保持连接
            print("客户端运行中，按 Ctrl+C 停止...")
            while True:
                await asyncio.sleep(1)

        finally:
            # 断开连接
            await client.disconnect()

    asyncio.run(example_usage())