"""
租户消息服务器 - 支持多租户连接管理和消息广播
"""

import asyncio
import json
import logging
import uuid
from typing import Dict, List, Optional, Set, Any, Callable
from dataclasses import dataclass
from collections import defaultdict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
import uvicorn

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


@dataclass
class MessageResult:
    """消息发送结果"""

    success: bool  # 是否成功
    message_id: str  # 消息ID
    error_code: Optional[str] = None  # 错误码
    error_message: Optional[str] = None  # 错误信息
    retry_count: int = 0  # 重试次数
    timestamp: float = 0.0  # 时间戳


class TenantMessageServer:
    """租户消息服务器"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8091):
        self.app = FastAPI(title="Tenant Message Server", version="1.0.0")
        self.host = host
        self.port = port

        # 连接管理
        self.connections: Dict[str, TenantConnection] = {}  # uuid -> connection

        # 租户-平台容器：tenant_id -> platform -> Set[connection_uuid]
        self.tenant_platforms: Dict[str, Dict[str, Set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )

        # 重试池：tenant_id -> List[failed_message]
        self.retry_pools: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        # 消息结果缓存：message_id -> MessageResult
        self.message_results: Dict[str, MessageResult] = {}

        # 消息处理器
        self.message_handlers: List[Callable] = []

        # 设置路由
        self._setup_routes()

        # 后台任务
        self.background_tasks: Set[asyncio.Task] = set()

    def register_message_handler(self, handler: Callable):
        """注册消息处理函数"""
        if handler not in self.message_handlers:
            self.message_handlers.append(handler)

    async def process_message(self, message: Dict[str, Any]):
        """处理单条消息"""
        tasks = []

        # 处理全局处理器
        for handler in self.message_handlers:
            try:
                result = handler(message)
                if asyncio.iscoroutine(result):
                    task = asyncio.create_task(result)
                    tasks.append(task)
                    self.background_tasks.add(task)
                    task.add_done_callback(self.background_tasks.discard)
            except Exception as e:
                logger.error(f"处理消息时出错: {e}")
                import traceback
                logger.debug(traceback.format_exc())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _setup_routes(self):
        """设置API路由"""

        @self.app.websocket("/ws/{tenant_id}/{platform}")
        async def websocket_endpoint(
            websocket: WebSocket, tenant_id: str, platform: str
        ):
            await self._handle_websocket_connection(websocket, tenant_id, platform)

        @self.app.post("/api/v1/tenants/{tenant_id}/platforms/{platform}/broadcast")
        async def broadcast_to_platform(tenant_id: str, platform: str, message: dict):
            """向租户的指定平台广播消息"""
            return await self._broadcast_to_tenant_platform(
                tenant_id, platform, message
            )

        # 注意：服务端不支持向租户的所有平台广播，只支持特定平台组播
        # 如果需要向多个平台广播，请分别调用平台广播接口

        @self.app.get("/api/v1/tenants/{tenant_id}/connections")
        async def list_tenant_connections(tenant_id: str):
            """列出租户的所有连接"""
            connections = self._get_tenant_connections(tenant_id)
            return {
                "connections": [self._connection_to_dict(conn) for conn in connections]
            }

        @self.app.get("/api/v1/message/{message_id}/result")
        async def get_message_result(message_id: str):
            """获取消息发送结果"""
            result = self.message_results.get(message_id)
            if not result:
                raise HTTPException(status_code=404, detail="Message result not found")
            return result.__dict__

        @self.app.post("/api/v1/tenants/{tenant_id}/retry-pool/flush")
        async def flush_retry_pool(tenant_id: str):
            """清空租户的重试池"""
            self.retry_pools[tenant_id].clear()
            return {"message": "Retry pool flushed"}

    async def _handle_websocket_connection(
        self, websocket: WebSocket, tenant_id: str, platform: str
    ):
        """处理WebSocket连接"""
        await websocket.accept()

        # 生成连接UUID
        connection_uuid = str(uuid.uuid4())

        # 创建连接对象
        connection = TenantConnection(
            uuid=connection_uuid,
            tenant_id=tenant_id,
            platform=platform,
            websocket=websocket,
            metadata={},
            created_at=asyncio.get_event_loop().time(),
            last_active=asyncio.get_event_loop().time(),
        )

        # 注册连接
        self.connections[connection_uuid] = connection
        self.tenant_platforms[tenant_id][platform].add(connection_uuid)

        logger.info(f"租户 {tenant_id} 平台 {platform} 连接已建立: {connection_uuid}")

        try:
            # 发送连接确认
            await self._send_connection_confirmation(connection)

            # 处理消息循环
            await self._message_loop(connection)

        except WebSocketDisconnect:
            logger.info(f"租户 {tenant_id} 平台 {platform} 连接断开: {connection_uuid}")
        except Exception as e:
            logger.error(f"处理连接 {connection_uuid} 时出错: {e}")
        finally:
            # 清理连接
            await self._cleanup_connection(connection_uuid)

    async def _send_connection_confirmation(self, connection: TenantConnection):
        """发送连接确认消息"""
        confirmation = {
            "type": "connection_confirmed",
            "connection_uuid": connection.uuid,
            "tenant_id": connection.tenant_id,
            "platform": connection.platform,
            "timestamp": asyncio.get_event_loop().time(),
        }
        await connection.websocket.send_text(json.dumps(confirmation))

    async def _message_loop(self, connection: TenantConnection):
        """消息处理循环"""
        while True:
            try:
                # 接收消息
                data = await asyncio.wait_for(
                    connection.websocket.receive_text(),
                    timeout=30.0,  # 30秒超时
                )

                # 更新活跃时间
                connection.last_active = asyncio.get_event_loop().time()

                # 解析消息
                try:
                    message = json.loads(data)
                except json.JSONDecodeError:
                    await self._send_error_response(
                        connection, "invalid_json", "Invalid JSON format"
                    )
                    continue

                # 处理消息
                await self._process_message(connection, message)

            except asyncio.TimeoutError:
                # 发送心跳
                await self._send_heartbeat(connection)
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"处理消息时出错: {e}")
                await self._send_error_response(connection, "internal_error", str(e))

    async def _process_message(self, connection: TenantConnection, message: dict):
        """处理接收到的消息"""
        message_type = message.get("type")

        if message_type == "heartbeat":
            # 心跳响应
            await self._send_heartbeat_response(connection)
        elif message_type == "message_result":
            # 消息结果确认
            await self._handle_message_result(connection, message)
        elif message_type == "ping":
            # Ping响应
            await connection.websocket.send_text(json.dumps({"type": "pong"}))
        else:
            # 处理自定义消息类型
            # 为消息添加连接信息
            enhanced_message = {
                **message,
                "connection_info": {
                    "tenant_id": connection.tenant_id,
                    "platform": connection.platform,
                    "connection_uuid": connection.uuid,
                }
            }

            # 调用注册的消息处理器
            await self.process_message(enhanced_message)

            logger.debug(f"处理自定义消息类型: {message_type}")

    async def _send_error_response(
        self, connection: TenantConnection, error_code: str, error_message: str
    ):
        """发送错误响应"""
        error_response = {
            "type": "error",
            "error_code": error_code,
            "error_message": error_message,
            "timestamp": asyncio.get_event_loop().time(),
        }
        try:
            await connection.websocket.send_text(json.dumps(error_response))
        except Exception as e:
            logger.error(f"发送错误响应失败: {e}")

    async def _send_heartbeat(self, connection: TenantConnection):
        """发送心跳"""
        heartbeat = {"type": "heartbeat", "timestamp": asyncio.get_event_loop().time()}
        try:
            await connection.websocket.send_text(json.dumps(heartbeat))
        except Exception as e:
            logger.error(f"发送心跳失败: {e}")

    async def _send_heartbeat_response(self, connection: TenantConnection):
        """发送心跳响应"""
        response = {
            "type": "heartbeat_response",
            "timestamp": asyncio.get_event_loop().time(),
        }
        try:
            await connection.websocket.send_text(json.dumps(response))
        except Exception as e:
            logger.error(f"发送心跳响应失败: {e}")

    async def _handle_message_result(self, connection: TenantConnection, message: dict):
        """处理消息结果"""
        message_id = message.get("message_id")
        if message_id:
            # 存储消息结果
            result = MessageResult(
                success=message.get("success", False),
                message_id=message_id,
                error_code=message.get("error_code"),
                error_message=message.get("error_message"),
                timestamp=asyncio.get_event_loop().time(),
            )
            self.message_results[message_id] = result

            logger.info(f"收到消息结果: {message_id}, success: {result.success}")

    async def _broadcast_to_tenant_platform(
        self, tenant_id: str, platform: str, message: dict
    ) -> dict:
        """向租户的指定平台广播消息"""
        connection_uuids = self.tenant_platforms.get(tenant_id, {}).get(platform, set())

        if not connection_uuids:
            return {"success": False, "error": "No active connections"}

        # 为消息添加租户信息
        enhanced_message = {
            **message,
            "tenant_id": tenant_id,
            "platform": platform,
            "broadcast_timestamp": asyncio.get_event_loop().time(),
            "message_id": str(uuid.uuid4()),
        }

        success_count = 0
        failed_connections = []

        for conn_uuid in list(connection_uuids):  # 创建副本避免迭代时修改
            connection = self.connections.get(conn_uuid)
            if connection and connection.websocket.client_state.name == "CONNECTED":
                try:
                    await connection.websocket.send_text(json.dumps(enhanced_message))
                    success_count += 1
                except Exception as e:
                    logger.error(f"向连接 {conn_uuid} 发送消息失败: {e}")
                    failed_connections.append(conn_uuid)
                    # 添加到重试池
                    self._add_to_retry_pool(tenant_id, enhanced_message, [conn_uuid])
            else:
                # 连接已断开，从容器中移除
                failed_connections.append(conn_uuid)

        # 清理断开的连接
        for conn_uuid in failed_connections:
            self.tenant_platforms[tenant_id][platform].discard(conn_uuid)
            if conn_uuid in self.connections:
                del self.connections[conn_uuid]

        return {
            "success": success_count > 0,
            "success_count": success_count,
            "failed_count": len(failed_connections),
            "total_connections": len(connection_uuids),
            "message_id": enhanced_message["message_id"],
        }

    # 移除向租户所有平台广播的功能，因为设计要求只支持特定平台组播
    # 如果需要向多个平台广播，请分别调用 _broadcast_to_tenant_platform

    def _add_to_retry_pool(
        self, tenant_id: str, message: dict, failed_connections: List[str]
    ):
        """添加消息到重试池"""
        retry_item = {
            "message": message,
            "failed_connections": failed_connections,
            "timestamp": asyncio.get_event_loop().time(),
            "retry_count": 0,
        }
        self.retry_pools[tenant_id].append(retry_item)

        # 限制重试池大小
        max_pool_size = 100
        if len(self.retry_pools[tenant_id]) > max_pool_size:
            self.retry_pools[tenant_id] = self.retry_pools[tenant_id][-max_pool_size:]

    def _get_tenant_connections(self, tenant_id: str) -> List[TenantConnection]:
        """获取租户的所有连接"""
        connections = []
        connection_uuids = set()

        for _platform, uuids in self.tenant_platforms.get(tenant_id, {}).items():
            for conn_uuid in uuids:
                if conn_uuid not in connection_uuids:
                    connection = self.connections.get(conn_uuid)
                    if connection:
                        connections.append(connection)
                        connection_uuids.add(conn_uuid)

        return connections

    def _connection_to_dict(self, connection: TenantConnection) -> Dict[str, Any]:
        """转换连接为字典格式"""
        return {
            "uuid": connection.uuid,
            "tenant_id": connection.tenant_id,
            "platform": connection.platform,
            "created_at": connection.created_at,
            "last_active": connection.last_active,
            "connection_pool": connection.connection_pool,
            "metadata": connection.metadata,
            "is_connected": connection.websocket.client_state.name == "CONNECTED",
        }

    async def _cleanup_connection(self, connection_uuid: str):
        """清理连接"""
        connection = self.connections.get(connection_uuid)
        if connection:
            # 从租户平台容器中移除
            self.tenant_platforms[connection.tenant_id][connection.platform].discard(
                connection_uuid
            )

            # 从连接字典中移除
            del self.connections[connection_uuid]

            logger.info(f"连接已清理: {connection_uuid}")

    async def start_retry_worker(self):
        """启动重试工作器"""
        while True:
            try:
                await asyncio.sleep(5)  # 每5秒检查一次重试池

                for tenant_id, retry_items in list(self.retry_pools.items()):
                    if not retry_items:
                        continue

                    new_retry_items = []

                    for retry_item in retry_items:
                        # 检查是否超过最大重试次数
                        if retry_item["retry_count"] >= 3:
                            logger.warning(
                                f"租户 {tenant_id} 消息重试次数超限，放弃重试"
                            )
                            continue

                        # 尝试重新发送
                        retry_item["retry_count"] += 1
                        retry_item["timestamp"] = asyncio.get_event_loop().time()

                        message = retry_item["message"]
                        failed_connections = retry_item["failed_connections"]

                        # 重新尝试发送
                        success = await self._retry_send_message(
                            tenant_id, message, failed_connections
                        )

                        if not success:
                            new_retry_items.append(retry_item)

                    self.retry_pools[tenant_id] = new_retry_items

            except Exception as e:
                logger.error(f"重试工作器出错: {e}")

    async def _retry_send_message(
        self, tenant_id: str, message: dict, failed_connections: List[str]
    ) -> bool:
        """重试发送消息"""
        success = False

        for conn_uuid in failed_connections:
            connection = self.connections.get(conn_uuid)
            if connection and connection.websocket.client_state.name == "CONNECTED":
                try:
                    await connection.websocket.send_text(json.dumps(message))
                    success = True
                except Exception as e:
                    logger.warning(f"重试发送到连接 {conn_uuid} 失败: {e}")

        return success

    async def start_server(self):
        """启动服务器"""
        # 启动重试工作器
        retry_task = asyncio.create_task(self.start_retry_worker())
        self.background_tasks.add(retry_task)

        logger.info(f"租户消息服务器启动在 {self.host}:{self.port}")

        # 启动FastAPI服务器
        config = uvicorn.Config(
            app=self.app, host=self.host, port=self.port, log_level="info"
        )
        server = uvicorn.Server(config)
        await server.serve()

    def run(self):
        """运行服务器（同步接口）"""
        uvicorn.run(
            self.app, host=self.host, port=self.port, log_level="info"
        )


# 启动服务器的便捷函数
def run_tenant_server(host: str = "0.0.0.0", port: int = 8091):
    """运行租户消息服务器"""
    server = TenantMessageServer(host=host, port=port)
    server.run()


if __name__ == "__main__":
    run_tenant_server()
