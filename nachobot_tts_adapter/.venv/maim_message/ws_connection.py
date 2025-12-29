"""
WebSocket通信实现模块，提供基于WebSocket的服务器和客户端实现
"""

import asyncio
import logging
import ssl
import aiohttp
import uvicorn
import os
import traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Any, Callable, List, Set, Optional, Union

from .connection_interface import (
    ServerConnectionInterface,
    ClientConnectionInterface,
    BaseConnection,
)
from .log_utils import get_logger, configure_uvicorn_logging, get_uvicorn_log_config

logger = get_logger()


class WebSocketServer(BaseConnection, ServerConnectionInterface):
    """基于WebSocket的服务器实现"""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 18000,
        path: str = "/ws",
        app: Optional[FastAPI] = None,
        ssl_certfile: Optional[str] = None,
        ssl_keyfile: Optional[str] = None,
        enable_token: bool = False,
        enable_custom_uvicorn_logger: Optional[bool] = False,
        max_message_size: int = 104857600,  # 100MB 默认值
    ):
        super().__init__()
        self.host = host
        self.port = port
        self.path = path
        self.app = app or FastAPI()
        self.own_app = app is None
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile = ssl_keyfile
        self.enable_custom_uvicorn_logger = enable_custom_uvicorn_logger
        self.max_message_size = max_message_size

        # WebSocket连接管理
        self.active_websockets: Set[WebSocket] = set()
        self.platform_websockets: Dict[str, WebSocket] = {}

        # 令牌验证
        self.enable_token = enable_token
        self.valid_tokens: Set[str] = set()

        # 服务器实例
        self.server = None

        # 设置WebSocket路由
        self._setup_routes()
        # 获取最新的logger实例
        global logger
        logger = get_logger()

    def _setup_routes(self):
        """设置WebSocket路由"""

        @self.app.websocket(self.path)
        async def websocket_endpoint(websocket: WebSocket):
            """处理WebSocket连接"""
            await websocket.accept()

            # 获取平台标识
            platform = websocket.headers.get("platform", "unknown")

            # 如果开启了令牌验证，检查令牌
            if self.enable_token:
                auth_header = websocket.headers.get("authorization")
                if not auth_header or not await self.verify_token(auth_header):
                    await websocket.close(code=1008, reason="无效的令牌")
                    return

            # 记录连接
            self.active_websockets.add(websocket)
            if platform != "unknown":
                # 如果已存在相同平台的连接，关闭旧连接
                if platform in self.platform_websockets:
                    old_ws = self.platform_websockets[platform]
                    await old_ws.close(code=1000, reason="新连接取代")
                    if old_ws in self.active_websockets:
                        self.active_websockets.remove(old_ws)

                self.platform_websockets[platform] = websocket
                logger.info(f"平台 {platform} WebSocket已连接")
            else:
                logger.info("新WebSocket连接已建立")

            try:
                # 持续处理消息
                while True:
                    try:
                        message = await websocket.receive_json()
                        task = asyncio.create_task(self.process_message(message))
                        self.add_background_task(task)
                    except asyncio.TimeoutError:
                        logger.warning(f"平台 {platform} 接收消息超时")
                        continue
                    except ValueError as e:
                        logger.warning(f"平台 {platform} 接收到无效JSON数据: {e}")
                        continue
                    except KeyboardInterrupt:
                        # 手动中断，静默处理，不记录错误
                        logger.debug(f"平台 {platform} WebSocket连接因用户中断而关闭")
                    except asyncio.CancelledError:
                        # 任务被取消，通常是正常关闭流程
                        logger.debug(f"平台 {platform} WebSocket任务被取消")
                    except Exception as e:
                        error_str = str(e).lower()
                        if any(
                            keyword in error_str
                            for keyword in [
                                "10",
                                "1012",
                                "connection",
                                "closed",
                                "disconnect",
                            ]
                        ):
                            logger.debug(f"平台 {platform} WebSocket连接关闭: {e}")
                        else:
                            logger.error(f"平台 {platform} WebSocket处理错误: {e}")
                            import traceback

                            logger.debug(traceback.format_exc())
                        break

            except WebSocketDisconnect:
                logger.info(f"WebSocket连接断开: {platform}")
            except ConnectionResetError:
                logger.warning(f"平台 {platform} 连接被重置")
            except KeyboardInterrupt:
                # 手动中断，静默处理，不记录错误
                logger.debug(f"平台 {platform} WebSocket连接因用户中断而关闭")
            except asyncio.CancelledError:
                # 任务被取消，通常是正常关闭流程
                logger.debug(f"平台 {platform} WebSocket任务被取消")
            except Exception as e:
                # 检查是否是连接关闭相关的错误
                error_str = str(e).lower()
                if any(
                    keyword in error_str
                    for keyword in ["1012", "connection", "closed", "disconnect"]
                ):
                    logger.debug(f"平台 {platform} WebSocket连接关闭: {e}")
                else:
                    logger.error(f"平台 {platform} WebSocket处理错误: {e}")
                    import traceback

                    logger.debug(traceback.format_exc())
            finally:
                self._remove_websocket(websocket, platform)

    def _remove_websocket(self, websocket: WebSocket, platform: str):
        """从所有集合中移除websocket"""
        if websocket in self.active_websockets:
            self.active_websockets.remove(websocket)
        if platform in self.platform_websockets:
            if self.platform_websockets[platform] == websocket:
                del self.platform_websockets[platform]

    async def verify_token(self, token: str) -> bool:
        """验证令牌是否有效"""
        if not self.enable_token:
            return True
        return token in self.valid_tokens

    def add_valid_token(self, token: str):
        """添加有效令牌"""
        # logger.info(f"添加有效令牌: {token}")
        self.valid_tokens.add(token)

    def remove_valid_token(self, token: str):
        """移除有效令牌"""
        self.valid_tokens.discard(token)

    async def start(self):
        """异步方式启动服务器"""
        # 获取最新的logger引用
        global logger
        logger = get_logger()

        self._running = True

        # 如果使用外部应用，只需设置标志位，不启动uvicorn
        if not self.own_app:
            logger.info("使用外部FastAPI应用，仅注册WebSocket路由")
            return

        # 验证SSL证书文件是否存在
        if self.ssl_certfile and self.ssl_keyfile:
            import os

            if not os.path.exists(self.ssl_certfile):
                logger.error(f"SSL证书文件不存在: {self.ssl_certfile}")
                raise FileNotFoundError(f"SSL证书文件不存在: {self.ssl_certfile}")
            if not os.path.exists(self.ssl_keyfile):
                logger.error(f"SSL密钥文件不存在: {self.ssl_keyfile}")
                raise FileNotFoundError(f"SSL密钥文件不存在: {self.ssl_keyfile}")
            logger.info(
                f"已验证SSL文件: certfile={self.ssl_certfile}, keyfile={self.ssl_keyfile}"
            )

        # 配置服务器
        # 为uvicorn准备日志配置
        if self.enable_custom_uvicorn_logger:
            log_config = get_uvicorn_log_config()
            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                ssl_certfile=self.ssl_certfile,
                ssl_keyfile=self.ssl_keyfile,
                log_config=log_config,
                ws_max_size=self.max_message_size,  # 设置WebSocket最大消息大小为100MB
            )
            # 确保uvicorn日志系统使用我们的配置
            configure_uvicorn_logging()
        else:
            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                ssl_certfile=self.ssl_certfile,
                ssl_keyfile=self.ssl_keyfile,
                ws_max_size=self.max_message_size,  # 设置WebSocket最大消息大小为100MB
            )

        # 启动服务器
        self.server = uvicorn.Server(config)
        try:
            await self.server.serve()
        except Exception as e:
            logger.error(f"服务器启动失败: {e}")
            raise

    def run_sync(self):
        """同步方式运行服务器"""
        if not self.own_app:
            logger.info("使用外部FastAPI应用，仅注册WebSocket路由")
            self._running = True
            return

        # 验证并打印SSL配置信息
        if self.ssl_certfile and self.ssl_keyfile:
            import os

            if not os.path.exists(self.ssl_certfile):
                logger.error(f"SSL证书文件不存在: {self.ssl_certfile}")
                raise FileNotFoundError(f"SSL证书文件不存在: {self.ssl_certfile}")
            if not os.path.exists(self.ssl_keyfile):
                logger.error(f"SSL密钥文件不存在: {self.ssl_keyfile}")
                raise FileNotFoundError(f"SSL密钥文件不存在: {self.ssl_keyfile}")
            logger.info(
                f"启用SSL: certfile={self.ssl_certfile}, keyfile={self.ssl_keyfile}"
            )

        # 配置服务器
        # 为uvicorn准备日志配置
        if self.enable_custom_uvicorn_logger:
            log_config = get_uvicorn_log_config()
            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                ssl_certfile=self.ssl_certfile,
                ssl_keyfile=self.ssl_keyfile,
                log_config=log_config,
                ws_max_size=self.max_message_size,  # 设置WebSocket最大消息大小为100MB
            )
            # 确保uvicorn日志系统使用我们的配置
            configure_uvicorn_logging()
        else:
            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                ssl_certfile=self.ssl_certfile,
                ssl_keyfile=self.ssl_keyfile,
                ws_max_size=self.max_message_size,  # 设置WebSocket最大消息大小为100MB
            )

        server = uvicorn.Server(config)
        try:
            server.run()
        except Exception as e:
            logger.error(f"服务器运行失败: {e}")
            raise

    async def stop(self):
        """停止服务器"""
        if not self._running:
            return

        self._running = False

        # 关闭所有WebSocket连接
        for websocket in list(self.active_websockets):
            try:
                await websocket.close(code=1000, reason="服务器关闭")
            except Exception:
                pass

        self.active_websockets.clear()
        self.platform_websockets.clear()

        # 清理后台任务
        await self.cleanup_tasks()

        # 仅当使用内部应用且服务器实例存在时尝试关闭服务器
        if self.own_app and self.server:
            try:
                # 检查server是否有shutdown方法
                if hasattr(self.server, "shutdown"):
                    await self.server.shutdown()
                # 如果没有shutdown方法但有should_exit属性
                elif hasattr(self.server, "should_exit"):
                    self.server.should_exit = True
                    logger.info("已设置服务器退出标志")
            except Exception as e:
                logger.warning(f"关闭服务器时发生错误: {e}")
                # 不抛出异常，让程序能够继续执行其他清理工作

    async def broadcast_message(self, message: Dict[str, Any]):
        """广播消息给所有连接的客户端"""
        disconnected = set()
        for websocket in list(self.active_websockets):
            try:
                # 检查连接状态
                if websocket.client_state.value >= 3:  # CLOSED state
                    disconnected.add(websocket)
                    continue

                await websocket.send_json(message)
            except (WebSocketDisconnect, ConnectionResetError):
                disconnected.add(websocket)
            except Exception as e:
                logger.warning(f"广播消息失败: {e}")
                # 检查是否是连接相关的错误
                error_msg = str(e).lower()
                if any(
                    keyword in error_msg
                    for keyword in ["closed", "disconnect", "reset"]
                ):
                    disconnected.add(websocket)

        # 清理断开的连接
        for websocket in disconnected:
            if websocket in self.active_websockets:
                self.active_websockets.remove(websocket)
            # 从平台映射中移除
            platform_to_remove = None
            for platform, ws in self.platform_websockets.items():
                if ws == websocket:
                    platform_to_remove = platform
                    break
            if platform_to_remove:
                del self.platform_websockets[platform_to_remove]

    async def send_message(self, target: str, message: Dict[str, Any]) -> bool:
        """向指定平台发送消息"""
        if target not in self.platform_websockets:
            logger.warning(f"未找到目标平台: {target}")
            return False

        websocket = self.platform_websockets[target]

        # 检查WebSocket连接状态
        try:
            # 检查连接是否已关闭
            if websocket.client_state.value >= 3:  # CLOSED state
                logger.warning(f"平台 {target} 的WebSocket连接已关闭")
                self._remove_websocket(websocket, target)
                return False

            await websocket.send_json(message)
            return True
        except WebSocketDisconnect:
            logger.warning(f"平台 {target} WebSocket连接断开")
            self._remove_websocket(websocket, target)
            return False
        except ConnectionResetError:
            logger.warning(f"平台 {target} 连接被重置")
            self._remove_websocket(websocket, target)
            return False
        except Exception as e:
            # 检查是否是连接相关的错误
            error_msg = str(e).lower()
            if any(
                keyword in error_msg
                for keyword in ["1012", "closed", "disconnect", "reset", "connection"]
            ):
                logger.debug(f"平台 {target} 连接异常: {e}")
            else:
                logger.error(f"发送消息到平台 {target} 失败: {e}")

            # 检查是否需要移除连接
            if any(
                keyword in error_msg for keyword in ["closed", "disconnect", "reset"]
            ):
                self._remove_websocket(websocket, target)
            return False


class WebSocketClient(BaseConnection, ClientConnectionInterface):
    """基于WebSocket的客户端实现"""

    def __init__(self):
        super().__init__()

        # 连接配置
        self.url = None
        self.platform = None
        self.token = None
        self.ssl_verify = None
        self.headers = {}
        self.max_message_size = 104857600  # 100MB 默认值

        # WebSocket连接
        self.ws = None
        self.ws_connected = False
        self.session = None  # 保存ClientSession实例

        # 重连设置
        self.reconnect_interval = 1
        self.retry_count = 0

        # 心跳设置
        self.heartbeat_interval = 60  # 60秒心跳间隔

        # 连接监控任务
        self._monitor_task = None

    async def configure(
        self,
        url: str,
        platform: str,
        token: Optional[str] = None,
        ssl_verify: Optional[str] = None,
        max_message_size: Optional[int] = None,
    ):
        """配置连接参数"""
        self.url = url
        self.platform = platform
        self.token = token
        self.ssl_verify = ssl_verify
        if max_message_size is not None:
            self.max_message_size = max_message_size

        # 设置请求头
        self.headers = {"platform": platform}
        if token:
            self.headers["Authorization"] = str(token)

    async def connect(self) -> bool:
        """连接到WebSocket服务器"""
        # 获取最新的logger引用
        global logger
        logger = get_logger()

        if not self.url or not self.platform:
            raise ValueError("连接前必须先调用configure方法配置连接参数")

        # 设置SSL上下文
        ssl_context = None
        if self.url.startswith("wss://"):
            ssl_context = ssl.create_default_context()
            if self.ssl_verify:
                logger.info(f"使用证书验证: {self.ssl_verify}")
                ssl_context.load_verify_locations(self.ssl_verify)
            else:
                logger.warning("警告: 未使用证书验证，已禁用证书验证")
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

        try:
            logger.info(f"正在连接到 {self.url}")
            print("WS_CONNECT_TARGET =", self.url)
            logger.debug(f"使用的头部信息: {self.headers}")

            # 配置连接
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            connector = aiohttp.TCPConnector(
                ssl=ssl_context, enable_cleanup_closed=True
            )

            # 创建会话并连接
            if self.session and not self.session.closed:
                await self.session.close()

            self.session = aiohttp.ClientSession(
                connector=connector, timeout=timeout, headers=self.headers
            )

            # 设置WebSocket连接参数（兼容aiohttp 3.11.18）
            ws_kwargs = {
                "heartbeat": self.heartbeat_interval,  # 心跳间隔
                "autoping": True,  # 自动ping
                "compress": 15,  # 压缩级别
                "autoclose": True,  # 自动关闭
                "max_msg_size": self.max_message_size,  # 设置最大消息大小为100MB
            }

            self.ws = await self.session.ws_connect(self.url, **ws_kwargs)

            self.ws_connected = True
            self.retry_count = 0
            logger.info(f"已成功连接到 {self.url}")
            return True

        except aiohttp.ClientError as e:
            if isinstance(e, aiohttp.ClientConnectorError):
                logger.error(
                    f"无法建立连接: {e.strerror if hasattr(e, 'strerror') else str(e)}"
                )
                self.ws_connected = False
            elif isinstance(e, aiohttp.ClientSSLError):
                logger.error(f"SSL错误: {str(e)}")
                self.ws_connected = False
            else:
                logger.error(f"连接错误: {str(e)}")
                self.ws_connected = False

            # 确保在错误情况下关闭会话
            await self._cleanup_session()
            return False

        except Exception as e:
            logger.error(f"连接时发生未预期的错误: {str(e)}")
            logger.debug(traceback.format_exc())

            # 确保在错误情况下关闭会话
            await self._cleanup_session()
            return False

    async def _cleanup_session(self):
        """安全地清理会话"""
        if self.session and not self.session.closed:
            try:
                await self.session.close()
                # 等待连接器完全关闭
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.debug(f"清理会话时出错: {e}")
            finally:
                self.session = None

    async def start(self):
        """开始接收消息循环"""
        # 获取最新的logger引用
        global logger
        logger = get_logger()

        if not self.ws_connected:
            await self.connect()

        self._running = True

        # 启动连接监控任务
        self._monitor_task = asyncio.create_task(self._connection_monitor())
        self.add_background_task(self._monitor_task)

        while self._running:
            try:
                # 检查连接状态，如果未连接则尝试重连
                if not self.ws_connected or self.ws is None:
                    success = await self.connect()
                    if not success:
                        retry_delay = min(
                            5,
                            self.reconnect_interval * (2 ** min(self.retry_count, 5)),
                        )
                        logger.info(f"等待 {retry_delay} 秒后重试...")
                        await asyncio.sleep(retry_delay)
                        self.retry_count += 1
                        continue

                # 持续接收消息
                if not self.ws_connected or self.ws is None:
                    continue
                async for msg in self.ws:
                    if not self._running:
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = msg.json()
                            logger.debug(f"接收到消息: {data}")
                            task = asyncio.create_task(self.process_message(data))
                            self.add_background_task(task)
                        except Exception as e:
                            logger.error(f"处理消息时出错: {e}")
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"WebSocket连接错误: {self.ws.exception()}")
                        self.ws_connected = False
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        logger.warning("WebSocket连接已关闭")
                        self.ws_connected = False
                        break
                    elif msg.type == aiohttp.WSMsgType.PONG:
                        logger.debug("收到PONG响应")
                    elif msg.type == aiohttp.WSMsgType.PING:
                        logger.debug("收到PING，将自动响应PONG")

                # 如果到达这里，连接已关闭
                self.ws_connected = False

            except asyncio.CancelledError:
                # 任务被取消，通常是正常关闭流程，不记录错误
                logger.debug("WebSocket客户端任务被取消")
                raise
            except KeyboardInterrupt:
                # 手动中断，静默处理
                logger.debug("WebSocket客户端因用户中断而关闭")
                self.ws_connected = False
                break
            except aiohttp.ServerTimeoutError:
                logger.warning("WebSocket心跳超时，尝试重连")
                self.ws_connected = False
                self.retry_count += 1
            except Exception as e:
                # 检查是否是连接关闭相关的错误
                error_str = str(e).lower()
                if any(
                    keyword in error_str
                    for keyword in [
                        "1012",
                        "connection",
                        "closed",
                        "disconnect",
                        "reset",
                    ]
                ):
                    logger.debug(f"WebSocket连接关闭: {e}")
                else:
                    logger.error(f"WebSocket连接发生错误: {e}")
                self.ws_connected = False
                self.retry_count += 1
                # 清理可能损坏的连接
                await self._cleanup_connection()
            finally:
                # 确保连接状态正确更新
                if self.ws and (self.ws.closed or self.ws.exception()):
                    self.ws_connected = False

            # 等待重连
            if self._running and not self.ws_connected:
                retry_delay = min(
                    30, self.reconnect_interval * (2 ** min(self.retry_count, 5))
                )
                logger.info(f"等待 {retry_delay} 秒后重试...")
                await asyncio.sleep(retry_delay)

    async def _cleanup_connection(self):
        """清理损坏的连接"""
        try:
            if self.ws and not self.ws.closed:
                await self.ws.close()
        except Exception:
            pass

        try:
            if self.session and not self.session.closed:
                await self.session.close()
                await asyncio.sleep(0.1)  # 等待连接器关闭
        except Exception:
            pass
        finally:
            self.ws = None
            self.session = None
            self.ws_connected = False

    async def stop(self):
        """停止客户端"""
        logger.info("正在停止WebSocket客户端...")
        self._running = False

        # 关闭WebSocket连接
        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
                logger.debug("WebSocket连接已关闭")
                self.ws = None
            except Exception as e:
                logger.warning(f"关闭WebSocket时出错: {e}")

        # 取消监控任务
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # 先清理后台任务，避免在关闭连接时还有任务在运行
        await self.cleanup_tasks()

        # 关闭ClientSession (使用更安全的方式)
        if self.session and not self.session.closed:
            try:
                # 等待一小段时间让连接完全关闭
                await asyncio.sleep(0.1)
                await self.session.close()
                logger.debug("ClientSession已关闭")

                # 等待connector完全关闭
                if hasattr(self.session, "connector") and self.session.connector:
                    await asyncio.sleep(0.1)

            except Exception as e:
                logger.warning(f"关闭ClientSession时出错: {e}")

        # 重置状态
        self.ws_connected = False
        self.ws = None
        self.session = None
        self._monitor_task = None
        logger.info("WebSocket客户端已停止")

    async def send_message(self, target: str, message: Dict[str, Any]) -> bool:
        """发送消息到服务器"""
        # 检查连接状态
        if not self.is_connected():
            logger.warning("WebSocket未连接，无法发送消息")
            return False

        try:
            await self.ws.send_json(message)
            return True
        except ConnectionResetError:
            logger.warning("连接被重置，标记为断开")
            self.ws_connected = False
            return False
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            self.ws_connected = False
            return False

    def is_connected(self) -> bool:
        """
        判断当前连接是否有效（存活）

        Returns:
            bool: 连接是否有效
        """
        if not self.ws_connected:
            return False

        if self.ws is None:
            self.ws_connected = False
            return False

        if self.ws.closed:
            self.ws_connected = False
            return False

        # 检查是否有异常
        if self.ws.exception():
            self.ws_connected = False
            return False

        return True

    async def ping(self) -> bool:
        """发送ping消息检查连接健康状态"""
        if not self.is_connected():
            return False

        try:
            await self.ws.ping()
            return True
        except Exception as e:
            logger.warning(f"Ping失败: {e}")
            self.ws_connected = False
            return False

    async def reconnect(self) -> bool:
        """手动重连"""
        logger.info("尝试重新连接...")
        # 首先清理现有连接
        await self._cleanup_connection()
        # 重置重试计数
        self.retry_count = 0
        return await self.connect()

    async def _connection_monitor(self):
        """连接监控任务，定期检查连接状态"""
        while self._running:
            try:
                await asyncio.sleep(30)  # 每30秒检查一次

                if not self._running:
                    break

                if self.ws_connected and self.ws:
                    # 检查连接是否真的有效
                    if self.ws.closed or self.ws.exception():
                        logger.warning("检测到连接异常，标记为断开")
                        self.ws_connected = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"连接监控任务出错: {e}")
