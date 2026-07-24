"""WebSocket服务端网络驱动器 - 纯网络I/O层，不处理业务逻辑"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional, Set
from enum import Enum

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)


class EventType(Enum):
    """事件类型"""

    CONNECT = "connect"
    DISCONNECT = "disconnect"
    MESSAGE = "message"


@dataclass
class ConnectionMetadata:
    """连接元数据"""

    uuid: str
    api_key: str
    platform: str
    headers: Dict[str, str]
    client_ip: Optional[str] = None
    connected_at: float = 0.0

    def __post_init__(self) -> None:
        if self.connected_at == 0.0:
            self.connected_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        # 清理headers中的敏感信息
        if "authorization" in result["headers"]:
            result["headers"] = {
                k: v
                for k, v in result["headers"].items()
                if k.lower() != "authorization"
            }
        return result


@dataclass
class NetworkEvent:
    """网络事件"""

    event_type: EventType
    uuid: str
    metadata: ConnectionMetadata
    payload: Optional[Dict[str, Any]] = None
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp == 0.0:
            self.timestamp = time.time()


class ServerNetworkDriver:
    """服务端网络驱动器 - 纯I/O层，负责WebSocket连接管理"""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 18000,
        path: str = "/ws",
        ssl_enabled: bool = False,
        ssl_certfile: str = None,
        ssl_keyfile: str = None,
        ssl_ca_certs: str = None,
        ssl_verify: bool = False,
    ):
        self.host = host
        self.port = port
        self.path = path

        # SSL配置
        self.ssl_enabled = ssl_enabled
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile = ssl_keyfile
        self.ssl_ca_certs = ssl_ca_certs
        self.ssl_verify = ssl_verify

        # 连接管理
        self.active_connections: Dict[str, WebSocket] = {}
        self.connection_metadata: Dict[str, ConnectionMetadata] = {}

        # 跨线程通信
        self.event_queue: Optional[asyncio.Queue] = None
        self.main_loop: Optional[asyncio.AbstractEventLoop] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.running = False

        # FastAPI应用
        self.app = FastAPI()
        self._setup_routes()

        # 统计信息
        self.stats = {
            "total_connections": 0,
            "current_connections": 0,
            "messages_received": 0,
            "messages_sent": 0,
            "bytes_received": 0,
            "bytes_sent": 0,
        }

        # 优雅关闭支持
        self._shutdown_event = asyncio.Event()
        self._server_task: Optional[asyncio.Task] = None
        self._uvicorn_server: Optional[uvicorn.Server] = None

    def _setup_routes(self) -> None:
        """设置WebSocket路由"""

        @self.app.websocket(self.path)
        async def websocket_endpoint(
            websocket: WebSocket, api_key: str = None, platform: str = None
        ):
            await self._handle_connection(
                websocket, query_api_key=api_key, query_platform=platform
            )

    async def _handle_connection(
        self,
        websocket: WebSocket,
        query_api_key: str = None,
        query_platform: str = None,
    ) -> None:
        """处理WebSocket连接的完整生命周期"""
        # 1. 接受连接
        await websocket.accept()

        # 2. 提取连接元数据
        metadata = self._extract_metadata(
            websocket, query_api_key=query_api_key, query_platform=query_platform
        )
        connection_uuid = metadata.uuid

        logger.info(f"New connection from {metadata.client_ip}: {connection_uuid}")

        # 3. 存储连接
        self.active_connections[connection_uuid] = websocket
        self.connection_metadata[connection_uuid] = metadata

        # 4. 更新统计
        self.stats["total_connections"] += 1
        self.stats["current_connections"] += 1

        # 5. 发送连接事件到业务层
        await self._send_event(EventType.CONNECT, connection_uuid)

        try:
            # 6. 消息处理循环 - 优雅处理服务器关闭
            while self.running and not self._shutdown_event.is_set():
                try:
                    # 接收文本消息，带超时以避免无限等待
                    message = await asyncio.wait_for(
                        websocket.receive_text(), timeout=1.0
                    )
                    await self._handle_message(connection_uuid, message)
                except asyncio.TimeoutError:
                    # 超时是正常的，继续循环检查running状态
                    continue
                except WebSocketDisconnect:
                    break
                except asyncio.CancelledError:
                    # 服务器关闭时的协程取消，正常退出
                    logger.debug(f"Connection task cancelled for {connection_uuid}")
                    break
                except Exception as e:
                    logger.debug(
                        f"Error receiving message from {connection_uuid}: {type(e).__name__}: {str(e)}"
                    )
                    break

        except WebSocketDisconnect:
            logger.debug(f"Connection disconnected: {connection_uuid}")
        except asyncio.CancelledError:
            # 服务器关闭时的协程取消，正常退出
            logger.debug(f"Connection handler cancelled for {connection_uuid}")
        except Exception as e:
            logger.debug(
                f"Connection error {connection_uuid}: {type(e).__name__}: {str(e)}"
            )
        finally:
            # 7. 清理连接
            await self._cleanup_connection(connection_uuid)

    def _extract_metadata(
        self,
        websocket: WebSocket,
        query_api_key: str = None,
        query_platform: str = None,
    ) -> ConnectionMetadata:
        """从WebSocket连接中提取元数据"""
        headers = dict(websocket.headers)

        # 提取必需的header，优先使用查询参数
        x_uuid = headers.get("x-uuid") or str(uuid.uuid4())
        x_apikey = query_api_key or headers.get("x-apikey", "")
        x_platform = query_platform or headers.get("x-platform", "unknown")

        # 获取客户端IP
        client_ip = websocket.client.host if websocket.client else "unknown"

        return ConnectionMetadata(
            uuid=x_uuid,
            api_key=x_apikey,
            platform=x_platform,
            headers=headers,
            client_ip=client_ip,
        )

    async def _handle_message(self, connection_uuid: str, message: Any) -> None:
        """处理接收到的消息"""
        try:
            # 更新统计
            self.stats["messages_received"] += 1
            if isinstance(message, str):
                self.stats["bytes_received"] += len(message.encode("utf-8"))

            # 解析JSON消息
            if isinstance(message, str):
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    # 如果不是JSON，包装成JSON
                    data = {"raw_message": message}
            else:
                data = message if isinstance(message, dict) else {"data": str(message)}

            # 立即发送ACK确认（如果需要）
            msg_id = data.get("msg_id")
            if msg_id and data.get("type") != "sys_ack":
                await self._send_ack(connection_uuid, msg_id)

            # 发送消息事件到业务层
            await self._send_event(EventType.MESSAGE, connection_uuid, data)

        except Exception as e:
            logger.error(f"Error handling message from {connection_uuid}: {e}")

    async def _send_ack(self, connection_uuid: str, msg_id: str) -> None:
        """发送消息确认"""
        try:
            ack_message = {
                "ver": 1,
                "msg_id": str(uuid.uuid4()),
                "type": "sys_ack",
                "meta": {
                    "uuid": connection_uuid,
                    "acked_msg_id": msg_id,
                    "timestamp": time.time(),
                },
                "payload": {"status": "received", "server_timestamp": time.time()},
            }

            await self._send_raw_message(connection_uuid, ack_message)

        except Exception as e:
            logger.error(f"Error sending ACK to {connection_uuid}: {e}")

    async def _send_event(
        self,
        event_type: EventType,
        connection_uuid: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """发送事件到业务层"""
        logger.debug(
            f"📤 Sending event {event_type.value} for connection {connection_uuid}"
        )

        try:
            metadata = self.connection_metadata.get(connection_uuid)
            if not metadata:
                # 对于清理事件，即使没有元数据也要创建基本的事件对象
                if event_type == EventType.DISCONNECT:
                    # 创建最基本的元数据用于清理事件
                    metadata = ConnectionMetadata(
                        uuid=connection_uuid,
                        api_key="",
                        platform="unknown",
                        headers={},
                        client_ip="unknown",
                    )
                    logger.debug(
                        f"🔧 Created minimal metadata for cleanup: {connection_uuid}"
                    )
                else:
                    logger.error(
                        f"❌ No metadata for connection {connection_uuid} - cannot send {event_type.value}"
                    )
                    logger.debug(
                        f"Available connections: {list(self.connection_metadata.keys())}"
                    )
                    return

            logger.debug(
                f"✅ Found metadata for {connection_uuid}: api_key={metadata.api_key}, platform={metadata.platform}"
            )

            event = NetworkEvent(
                event_type=event_type,
                uuid=connection_uuid,
                metadata=metadata,
                payload=payload,
            )

            logger.debug(
                f"🚀 Created NetworkEvent {event_type.value} for {connection_uuid}"
            )

            # 直接发送事件到队列（同一线程）
            if self.event_queue:
                await self.event_queue.put(event)
                logger.debug(
                    f"✅ Event {event_type.value} for {connection_uuid} sent successfully"
                )
            else:
                logger.warning(
                    f"⚠️ Event queue is None, dropping event {event_type.value} for {connection_uuid}"
                )

        except Exception as e:
            logger.error(f"❌ Error sending event to business layer: {e}")
            logger.error(f"   Event type: {event_type.value}")
            logger.error(f"   Connection UUID: {connection_uuid}")
            import traceback

            logger.error(f"   Traceback: {traceback.format_exc()}")

    async def _cleanup_connection(self, connection_uuid: str) -> None:
        """清理连接资源"""
        try:
            # 在清理元数据之前，先获取元数据用于发送断开事件
            metadata = self.connection_metadata.get(connection_uuid)

            # 关闭WebSocket连接
            if connection_uuid in self.active_connections:
                websocket = self.active_connections[connection_uuid]
                try:
                    if websocket.client_state == WebSocketState.CONNECTED:
                        await websocket.close()
                except Exception as close_error:
                    logger.debug(
                        f"Error closing websocket {connection_uuid}: {close_error}"
                    )
                finally:
                    # 确保无论如何都删除连接
                    if connection_uuid in self.active_connections:
                        del self.active_connections[connection_uuid]

            # 先发送断开事件（此时还有元数据）
            if metadata:
                try:
                    await self._send_event(
                        EventType.DISCONNECT, connection_uuid, {"cleanup": True}
                    )
                except Exception as event_error:
                    logger.debug(
                        f"Error sending disconnect event {connection_uuid}: {event_error}"
                    )

            # 清理元数据
            if connection_uuid in self.connection_metadata:
                del self.connection_metadata[connection_uuid]

            # 安全地更新统计
            if self.stats.get("current_connections", 0) > 0:
                self.stats["current_connections"] -= 1

        except Exception as e:
            # 只记录调试信息，不输出错误日志
            logger.debug(
                f"Debug: connection cleanup {connection_uuid} error: {type(e).__name__}: {str(e)}"
            )

    async def _send_raw_message(
        self, connection_uuid: str, message: Dict[str, Any]
    ) -> bool:
        """发送原始消息到指定连接"""
        if connection_uuid not in self.active_connections:
            logger.warning(f"Connection {connection_uuid} not found")
            return False

        websocket = self.active_connections[connection_uuid]

        try:
            message_str = json.dumps(message)
            await websocket.send_text(message_str)

            # 更新统计
            self.stats["messages_sent"] += 1
            self.stats["bytes_sent"] += len(message_str.encode("utf-8"))

            return True

        except Exception as e:
            logger.error(f"Error sending message to {connection_uuid}: {e}")
            # 连接可能已断开，清理它
            await self._cleanup_connection(connection_uuid)
            return False

    async def send_message(self, connection_uuid: str, message: Dict[str, Any]) -> bool:
        """发送消息到指定连接（业务层接口）"""
        return await self._send_raw_message(connection_uuid, message)

    async def broadcast_message(
        self, message: Dict[str, Any], filter_func: Optional[callable] = None
    ) -> Dict[str, bool]:
        """广播消息到所有连接"""
        results = {}

        for connection_uuid, websocket in list(self.active_connections.items()):
            # 应用过滤器
            if filter_func:
                metadata = self.connection_metadata.get(connection_uuid)
                if not filter_func(metadata):
                    continue

            success = await self._send_raw_message(connection_uuid, message)
            results[connection_uuid] = success

        return results

    async def disconnect_client(
        self, connection_uuid: str, reason: str = "Server initiated disconnect"
    ) -> bool:
        """主动断开客户端连接"""
        if connection_uuid not in self.active_connections:
            return False

        try:
            websocket = self.active_connections[connection_uuid]
            await websocket.close(code=1000, reason=reason)
            await self._cleanup_connection(connection_uuid)
            return True
        except Exception as e:
            logger.error(f"Error disconnecting client {connection_uuid}: {e}")
            return False

    def get_connection_count(self) -> int:
        """获取当前连接数"""
        return len(self.active_connections)

    def get_connection_list(self) -> Set[str]:
        """获取所有连接UUID"""
        return set(self.active_connections.keys())

    def get_connection_metadata(
        self, connection_uuid: str
    ) -> Optional[ConnectionMetadata]:
        """获取连接元数据"""
        return self.connection_metadata.get(connection_uuid)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return self.stats.copy()

    async def _server_loop_run(self, event_queue: asyncio.Queue) -> None:
        """在主事件循环中运行服务器"""
        try:
            # 设置事件队列引用
            self.event_queue = event_queue

            # 创建uvicorn配置
            uvicorn_kwargs = {
                "app": self.app,
                "host": self.host,
                "port": self.port,
                "log_level": "warning",  # 减少uvicorn日志
            }

            # 添加SSL配置
            if self.ssl_enabled:
                if not self.ssl_certfile or not self.ssl_keyfile:
                    raise ValueError(
                        "SSL enabled but ssl_certfile or ssl_keyfile not provided"
                    )

                uvicorn_kwargs.update(
                    {
                        "ssl_certfile": self.ssl_certfile,
                        "ssl_keyfile": self.ssl_keyfile,
                    }
                )

                # 可选的CA证书和验证配置
                if self.ssl_ca_certs:
                    uvicorn_kwargs["ssl_ca_certs"] = self.ssl_ca_certs

                if self.ssl_verify:
                    uvicorn_kwargs["ssl_cert_reqs"] = 2  # ssl.CERT_REQUIRED

            config = uvicorn.Config(**uvicorn_kwargs)

            # 启动服务器
            self._uvicorn_server = uvicorn.Server(config)
            self.running = True

            # 创建服务器任务但不直接await，这样可以控制关闭
            self._server_task = asyncio.create_task(self._uvicorn_server.serve())

            # 等待关闭信号
            await self._shutdown_event.wait()

            # 优雅关闭服务器
            if self._uvicorn_server and not self._uvicorn_server.should_exit:
                self._uvicorn_server.should_exit = True

            # 等待服务器任务完成
            if self._server_task:
                await self._server_task

        except asyncio.CancelledError:
            # 正常的取消，不需要记录为错误
            logger.info("Server task cancelled gracefully")
        except Exception as e:
            logger.error(f"Server loop error: {e}")
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")
        finally:
            self.running = False
            self._uvicorn_server = None
            self._server_task = None

    async def start(self, event_queue: asyncio.Queue) -> None:
        """启动网络驱动器"""
        if self.running:
            logger.warning("Network driver already running")
            return

        # 在主事件循环中启动服务器
        logger.info(f"Starting network driver on {self.host}:{self.port}{self.path}")
        await self._server_loop_run(event_queue)

    async def stop(self) -> None:
        """停止网络驱动器 - 完全清理所有协程"""
        if not self.running:
            return

        logger.info("Stopping network driver...")
        self.running = False

        # 1. 首先发送关闭信号给服务器循环
        self._shutdown_event.set()

        # 2. 首先等待一点时间让连接处理循环自然退出
        await asyncio.sleep(0.1)

        # 3. 主动断开所有活跃连接
        connection_uuids = list(self.active_connections.keys())
        for connection_uuid in connection_uuids:
            try:
                # 直接清理连接，不发送断开事件
                if connection_uuid in self.active_connections:
                    websocket = self.active_connections[connection_uuid]
                    try:
                        # 强制关闭WebSocket连接
                        if (
                            hasattr(websocket, "client_state")
                            and websocket.client_state != WebSocketState.DISCONNECTED
                        ):
                            websocket.close(code=1000, reason="Server shutdown")
                    except Exception:
                        pass
                    finally:
                        # 确保清理连接映射
                        if connection_uuid in self.active_connections:
                            del self.active_connections[connection_uuid]

                # 清理连接元数据
                if connection_uuid in self.connection_metadata:
                    del self.connection_metadata[connection_uuid]

            except Exception as e:
                logger.debug(f"Error during shutdown cleanup {connection_uuid}: {e}")
                # 确保无论如何都清理连接
                try:
                    if connection_uuid in self.active_connections:
                        del self.active_connections[connection_uuid]
                    if connection_uuid in self.connection_metadata:
                        del self.connection_metadata[connection_uuid]
                except Exception:
                    pass

        # 4. 请求uvicorn服务器优雅退出
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True

        # 5. 等待服务器任务优雅退出，仅在卡住时才取消
        if self._server_task and not self._server_task.done():
            try:
                await asyncio.wait_for(self._server_task, timeout=5.0)
            except asyncio.TimeoutError:
                # 只有在无法正常退出时才取消，避免触发lifespan CancelledError
                self._server_task.cancel()
                try:
                    await self._server_task
                except asyncio.CancelledError:
                    pass

        # 6. 重置所有状态
        self.active_connections.clear()
        self.connection_metadata.clear()
        self._server_task = None
        self._uvicorn_server = None
        self.event_queue = None
        self.main_loop = None
        self._shutdown_event = asyncio.Event()

        # 7. 重置统计信息
        self.stats = {
            "total_connections": 0,
            "current_connections": 0,
            "messages_received": 0,
            "messages_sent": 0,
            "bytes_received": 0,
            "bytes_sent": 0,
        }

        logger.info("Network driver stopped completely")
