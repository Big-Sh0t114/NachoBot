"""
线程化租户消息服务器 - 为每个WebSocket连接单独开线程，彻底杜绝协程堵塞
"""

import asyncio
import json
import logging
import uuid
import threading
import time
from typing import Dict, List, Optional, Set, Any
from dataclasses import dataclass, field
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
import uvicorn
from queue import Queue, Empty
import weakref

logger = logging.getLogger(__name__)


@dataclass
class TenantConnection:
    """租户连接信息"""

    uuid: str  # 连接唯一标识
    tenant_id: str  # 租户ID
    platform: str  # 平台标识
    websocket: WebSocket  # WebSocket连接
    metadata: Dict[str, Any]  # 元数据
    created_at: float  # 创建时间
    last_active: float  # 最后活跃时间
    connection_pool: Optional[str] = None  # 连接池标识
    api_key: Optional[str] = None  # API密钥
    message_queue: Queue = field(default_factory=Queue)  # 消息队列
    thread_id: Optional[int] = None  # 处理线程ID
    is_running: bool = True  # 连接是否运行中


@dataclass
class MessageResult:
    """消息发送结果"""

    success: bool  # 是否成功
    message_id: str  # 消息ID
    error_code: Optional[str] = None  # 错误码
    error_message: Optional[str] = None  # 错误信息
    retry_count: int = 0  # 重试次数
    timestamp: float = 0.0  # 时间戳


class ConnectionThreadManager:
    """连接线程管理器 - 管理每个连接的专用线程"""

    def __init__(self):
        self.connection_threads: Dict[str, threading.Thread] = {}
        self.connection_loops: Dict[str, weakref.ref] = {}
        self.executor = ThreadPoolExecutor(max_workers=1000, thread_name_prefix="ConnWorker")

    def start_connection_thread(self, connection: TenantConnection, server_ref) -> bool:
        """为连接启动专用线程"""
        try:
            # 创建弱引用避免循环引用
            connection_ref = weakref.ref(connection)

            # 创建并启动线程
            thread = threading.Thread(
                target=self._connection_worker,
                args=(connection_ref, server_ref),
                name=f"WS-Conn-{connection.tenant_id}-{connection.platform}-{connection.uuid[:8]}",
                daemon=True
            )

            thread.start()
            connection.thread_id = thread.ident
            connection.is_running = True

            self.connection_threads[connection.uuid] = thread
            self.connection_loops[connection.uuid] = connection_ref

            logger.info(f"连接线程已启动: {connection.uuid} (线程ID: {thread.ident})")
            return True

        except Exception as e:
            logger.error(f"启动连接线程失败: {e}")
            return False

    def _connection_worker(self, connection_ref: weakref.ref, server_ref):
        """连接工作线程 - 每个连接的专用处理循环"""
        connection = connection_ref()
        if not connection:
            return

        try:
            # 在线程中创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 运行连接处理逻辑
            loop.run_until_complete(self._handle_connection_loop(connection, connection_ref, server_ref))

        except Exception as e:
            logger.error(f"连接线程异常 {connection.uuid}: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

            # 清理线程记录
            if connection.uuid in self.connection_threads:
                del self.connection_threads[connection.uuid]
            if connection.uuid in self.connection_loops:
                del self.connection_loops[connection.uuid]

            logger.info(f"连接线程已退出: {connection.uuid}")

    async def _handle_connection_loop(self, connection: TenantConnection, connection_ref: weakref.ref, server_ref):
        """处理连接的消息循环"""
        try:
            # 发送连接确认
            await self._send_connection_confirmation(connection)

            # 主消息处理循环
            while connection.is_running:
                try:
                    # 处理WebSocket消息
                    await self._process_websocket_message(connection, server_ref)

                    # 处理队列中的待发送消息
                    await self._process_message_queue(connection)

                    # 发送心跳
                    await self._send_heartbeat_if_needed(connection)

                    # 短暂休眠避免CPU占用过高
                    await asyncio.sleep(0.01)

                except WebSocketDisconnect:
                    logger.info(f"WebSocket连接断开: {connection.uuid}")
                    break
                except Exception as e:
                    logger.error(f"消息处理异常 {connection.uuid}: {e}")
                    await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"连接处理循环异常 {connection.uuid}: {e}")
        finally:
            connection.is_running = False

    async def _send_connection_confirmation(self, connection: TenantConnection):
        """发送连接确认"""
        try:
            confirmation = {
                "type": "connection_confirmation",
                "connection_uuid": connection.uuid,
                "tenant_id": connection.tenant_id,
                "platform": connection.platform,
                "timestamp": time.time(),
                "server_info": {
                    "version": "1.0.0",
                    "threaded": True
                }
            }
            await connection.websocket.send_text(json.dumps(confirmation))
        except Exception as e:
            logger.error(f"发送连接确认失败: {e}")

    async def _process_websocket_message(self, connection: TenantConnection, server_ref):
        """处理WebSocket接收到的消息"""
        try:
            # 使用短超时避免阻塞
            message = await asyncio.wait_for(
                connection.websocket.receive_text(),
                timeout=0.1
            )

            connection.last_active = time.time()

            try:
                data = json.loads(message)

                # 处理消息
                server = server_ref()
                if server:
                    await server._process_received_message(connection, data)

            except json.JSONDecodeError:
                await self._send_error_response(connection, "invalid_json", "Invalid JSON format")

        except asyncio.TimeoutError:
            # 超时是正常的，继续处理其他任务
            pass
        except WebSocketDisconnect:
            raise
        except Exception as e:
            logger.error(f"WebSocket消息处理异常: {e}")

    async def _process_message_queue(self, connection: TenantConnection):
        """处理待发送消息队列"""
        try:
            while not connection.message_queue.empty():
                try:
                    message_data = connection.message_queue.get_nowait()

                    # 发送消息
                    await connection.websocket.send_text(json.dumps(message_data))
                    connection.last_active = time.time()

                except Empty:
                    break
                except Exception as e:
                    logger.error(f"发送队列消息失败: {e}")

        except Exception as e:
            logger.error(f"处理消息队列异常: {e}")

    async def _send_heartbeat_if_needed(self, connection: TenantConnection):
        """根据需要发送心跳"""
        current_time = time.time()
        if current_time - connection.last_active > 30:  # 30秒无活动发送心跳
            try:
                heartbeat = {
                    "type": "heartbeat",
                    "timestamp": current_time,
                    "server_heartbeat": True
                }
                await connection.websocket.send_text(json.dumps(heartbeat))
                connection.last_active = current_time
            except Exception as e:
                logger.error(f"发送心跳失败: {e}")

    async def _send_error_response(self, connection: TenantConnection, error_code: str, error_message: str):
        """发送错误响应"""
        try:
            error_response = {
                "type": "error",
                "error_code": error_code,
                "error_message": error_message,
                "timestamp": time.time()
            }
            await connection.websocket.send_text(json.dumps(error_response))
        except Exception as e:
            logger.error(f"发送错误响应失败: {e}")

    def stop_connection_thread(self, connection_uuid: str):
        """停止连接线程"""
        connection_ref = self.connection_loops.get(connection_uuid)
        if connection_ref:
            connection = connection_ref()
            if connection:
                connection.is_running = False

        thread = self.connection_threads.get(connection_uuid)
        if thread and thread.is_alive():
            # 等待线程自然结束
            thread.join(timeout=5.0)

    def shutdown(self):
        """关闭所有连接线程"""
        logger.info("正在关闭所有连接线程...")

        # 停止所有连接
        for connection_uuid in list(self.connection_threads.keys()):
            self.stop_connection_thread(connection_uuid)

        # 关闭线程池
        self.executor.shutdown(wait=True)
        logger.info("所有连接线程已关闭")


class TenantMessageServerThreaded:
    """线程化租户消息服务器"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8091):
        self.app = FastAPI(title="Tenant Message Server (Threaded)", version="1.0.0")
        self.host = host
        self.port = port

        # 连接管理
        self.connections: Dict[str, TenantConnection] = {}
        self.tenant_platforms: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))

        # 消息处理
        self.message_results: Dict[str, MessageResult] = {}
        self.retry_pools: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        # 线程管理
        self.thread_manager = ConnectionThreadManager()
        self._running = True

        # 设置路由
        self._setup_routes()

    def _setup_routes(self):
        """设置API路由"""

        @self.app.websocket("/ws/{tenant_id}/{platform}")
        async def websocket_endpoint(websocket: WebSocket, tenant_id: str, platform: str):
            """WebSocket连接端点"""
            await self._handle_websocket_connection(websocket, tenant_id, platform)

        @self.app.post("/api/v1/tenants/{tenant_id}/platforms/{platform}/broadcast")
        async def broadcast_to_tenant_platform(tenant_id: str, platform: str, message: Dict[str, Any]):
            """向指定租户平台广播消息"""
            return await self._broadcast_to_tenant_platform(tenant_id, platform, message)

        @self.app.get("/api/v1/tenants/{tenant_id}/connections")
        async def get_tenant_connections(tenant_id: str):
            """获取租户的所有连接"""
            return await self._get_tenant_connections(tenant_id)

        @self.app.get("/api/v1/message/{message_id}/result")
        async def get_message_result(message_id: str):
            """获取消息结果"""
            return await self._get_message_result(message_id)

        @self.app.post("/api/v1/tenants/{tenant_id}/retry-pool/flush")
        async def flush_retry_pool(tenant_id: str):
            """刷新重试池"""
            return await self._flush_retry_pool(tenant_id)

    async def _handle_websocket_connection(self, websocket: WebSocket, tenant_id: str, platform: str):
        """处理WebSocket连接"""
        connection_uuid = str(uuid.uuid4())
        current_time = time.time()

        try:
            await websocket.accept()

            # 创建连接对象
            connection = TenantConnection(
                uuid=connection_uuid,
                tenant_id=tenant_id,
                platform=platform,
                websocket=websocket,
                metadata={},
                created_at=current_time,
                last_active=current_time
            )

            # 存储连接
            self.connections[connection_uuid] = connection
            self.tenant_platforms[tenant_id][platform].add(connection_uuid)

            logger.info(f"租户 {tenant_id} 平台 {platform} 连接已建立: {connection_uuid}")

            # 启动连接的专用线程
            if self.thread_manager.start_connection_thread(connection, weakref.ref(self)):
                # 等待连接线程结束
                while connection.is_running:
                    await asyncio.sleep(1)
            else:
                logger.error(f"无法启动连接线程: {connection_uuid}")
                await self._cleanup_connection(connection_uuid)

        except Exception as e:
            logger.error(f"WebSocket连接处理异常: {e}")
            await self._cleanup_connection(connection_uuid)

    async def _process_received_message(self, connection: TenantConnection, data: Dict[str, Any]):
        """处理接收到的消息"""
        message_type = data.get("type", "unknown")

        if message_type == "ping":
            # 响应ping
            pong_message = {
                "type": "pong",
                "timestamp": time.time(),
                "server_pong": True,
                "connection_uuid": connection.uuid
            }
            connection.message_queue.put(pong_message)

        elif message_type == "chat_message":
            # 处理聊天消息
            await self._handle_chat_message(connection, data)

        elif message_type == "heartbeat_response":
            # 心跳响应，更新活跃时间
            connection.last_active = time.time()

        else:
            # 其他消息类型
            logger.info(f"收到消息类型 {message_type} 来自 {connection.uuid}")

    async def _handle_chat_message(self, connection: TenantConnection, message: Dict[str, Any]):
        """处理聊天消息"""
        content = message.get("content", "")
        message_id = message.get("message_id", str(uuid.uuid4()))

        logger.info(f"处理聊天消息: {content} (来自 {connection.uuid})")

        # 模拟处理
        processed_content = f"[已处理] {content}"

        # 发送回复
        response = {
            "type": "chat_response",
            "message_id": message_id,
            "original_content": content,
            "processed_content": processed_content,
            "timestamp": time.time(),
            "server_note": "此消息由线程化租户消息服务器处理",
            "connection_uuid": connection.uuid
        }

        connection.message_queue.put(response)

        # 保存消息结果
        self.message_results[message_id] = MessageResult(
            success=True,
            message_id=message_id,
            timestamp=time.time()
        )

    async def _broadcast_to_tenant_platform(self, tenant_id: str, platform: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """向租户平台广播消息"""
        message_id = str(uuid.uuid4())
        current_time = time.time()

        # 获取目标连接
        target_connections = self.tenant_platforms.get(tenant_id, {}).get(platform, set())

        success_count = 0
        failed_count = 0

        for connection_uuid in list(target_connections):
            connection = self.connections.get(connection_uuid)
            if connection and connection.is_running:
                try:
                    # 为广播消息添加额外信息
                    broadcast_message = {
                        **message,
                        "message_id": message_id,
                        "broadcast_info": {
                            "tenant_id": tenant_id,
                            "platform": platform,
                            "timestamp": current_time,
                            "source": "server_broadcast"
                        }
                    }

                    connection.message_queue.put(broadcast_message)
                    success_count += 1

                except Exception as e:
                    logger.error(f"广播消息失败 {connection_uuid}: {e}")
                    failed_count += 1
            else:
                failed_count += 1

        result = {
            "message_id": message_id,
            "success_count": success_count,
            "failed_count": failed_count,
            "total_connections": len(target_connections),
            "timestamp": current_time
        }

        logger.info(f"广播完成: 租户 {tenant_id} 平台 {platform} - 成功 {success_count}/{len(target_connections)}")

        return result

    async def _get_tenant_connections(self, tenant_id: str) -> Dict[str, Any]:
        """获取租户连接信息"""
        connections = []

        for connection_uuid, connection in self.connections.items():
            if connection.tenant_id == tenant_id:
                connections.append({
                    "uuid": connection.uuid,
                    "platform": connection.platform,
                    "created_at": connection.created_at,
                    "last_active": connection.last_active,
                    "is_connected": connection.is_running,
                    "thread_id": connection.thread_id
                })

        return {
            "tenant_id": tenant_id,
            "connections": connections,
            "total_connections": len(connections)
        }

    async def _get_message_result(self, message_id: str) -> Dict[str, Any]:
        """获取消息结果"""
        result = self.message_results.get(message_id)
        if result:
            return {
                "message_id": message_id,
                "success": result.success,
                "error_code": result.error_code,
                "error_message": result.error_message,
                "retry_count": result.retry_count,
                "timestamp": result.timestamp
            }
        else:
            raise HTTPException(status_code=404, detail="Message result not found")

    async def _flush_retry_pool(self, tenant_id: str) -> Dict[str, Any]:
        """刷新重试池"""
        if tenant_id in self.retry_pools:
            retry_count = len(self.retry_pools[tenant_id])
            self.retry_pools[tenant_id].clear()
            return {
                "tenant_id": tenant_id,
                "flushed_messages": retry_count,
                "timestamp": time.time()
            }
        else:
            return {
                "tenant_id": tenant_id,
                "flushed_messages": 0,
                "timestamp": time.time()
            }

    async def _cleanup_connection(self, connection_uuid: str):
        """清理连接"""
        connection = self.connections.get(connection_uuid)
        if connection:
            # 停止连接线程
            self.thread_manager.stop_connection_thread(connection_uuid)

            # 从租户平台映射中移除
            if connection.tenant_id in self.tenant_platforms:
                if connection.platform in self.tenant_platforms[connection.tenant_id]:
                    self.tenant_platforms[connection.tenant_id][connection.platform].discard(connection_uuid)

            # 移除连接
            del self.connections[connection_uuid]

            logger.info(f"连接已清理: {connection_uuid}")

    async def start_server(self):
        """启动服务器"""
        logger.info(f"线程化租户消息服务器启动在 {self.host}:{self.port}")

        try:
            config = uvicorn.Config(
                app=self.app, host=self.host, port=self.port, log_level="info"
            )
            server = uvicorn.Server(config)
            await server.serve()
        except Exception as e:
            logger.error(f"服务器启动失败: {e}")
        finally:
            self.shutdown()

    def run(self):
        """运行服务器（同步接口）"""
        uvicorn.run(
            self.app, host=self.host, port=self.port, log_level="info"
        )

    def shutdown(self):
        """关闭服务器"""
        logger.info("正在关闭线程化租户消息服务器...")

        self._running = False

        # 关闭所有连接线程
        self.thread_manager.shutdown()

        # 清理连接
        for connection_uuid in list(self.connections.keys()):
            asyncio.create_task(self._cleanup_connection(connection_uuid))

        logger.info("线程化租户消息服务器已关闭")


# 便捷函数
def run_threaded_tenant_server(host: str = "0.0.0.0", port: int = 8091):
    """运行线程化租户消息服务器"""
    server = TenantMessageServerThreaded(host, port)
    server.run()


if __name__ == "__main__":
    run_threaded_tenant_server()