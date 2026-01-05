"""WebSocket 通信模块，提供服务器和客户端实现。"""

from __future__ import annotations

import asyncio
import os
import ssl
from typing import Any, Dict, Optional, Set

import aiohttp
from aiohttp import WSMsgType
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from .connection_interface import (
    BaseConnection,
    ClientConnectionInterface,
    ServerConnectionInterface,
)
from .log_utils import configure_uvicorn_logging, get_logger, get_uvicorn_log_config

logger = get_logger()

_CONNECTION_ERROR_KEYWORDS = (
    "1000",
    "1001",
    "1006",
    "1011",
    "1012",
    "closed",
    "disconnect",
    "reset",
    "timeout",
    "broken pipe",
    "connection",
)


def _looks_like_connection_error(exc: BaseException) -> bool:
    """判断异常是否属于常见的连接断开场景。"""

    try:
        message = str(exc).lower()
    except Exception:  # pragma: no cover - 极少数情况下 str(exc) 失败
        return False
    return any(keyword in message for keyword in _CONNECTION_ERROR_KEYWORDS)


class WebSocketServer(BaseConnection, ServerConnectionInterface):
    """基于 WebSocket 的服务器实现。"""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 18000,
        path: str = "/ws",
        app: Optional[FastAPI] = None,
        *,
        ssl_certfile: Optional[str] = None,
        ssl_keyfile: Optional[str] = None,
        enable_token: bool = False,
        enable_custom_uvicorn_logger: bool = False,
        max_message_size: int = 104_857_600,
        heartbeat_interval: float = 30.0,
        heartbeat_timeout: float = 10.0,
    ) -> None:
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
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout

        self.enable_token = enable_token
        self.valid_tokens: Set[str] = set()

        self.active_websockets: Set[WebSocket] = set()
        self.platform_websockets: Dict[str, WebSocket] = {}

        self.server: Optional[uvicorn.Server] = None

        self._setup_routes()

    # ------------------------------------------------------------------
    # 路由与连接管理
    # ------------------------------------------------------------------
    def _setup_routes(self) -> None:
        @self.app.websocket(self.path)
        async def websocket_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()

            platform = websocket.headers.get("platform", "unknown")

            if self.enable_token:
                auth_header = websocket.headers.get("authorization")
                if not auth_header or not await self.verify_token(auth_header):
                    logger.warning(f"拒绝平台 {platform} 的连接请求: 令牌无效")
                    await websocket.close(code=1008, reason="无效的令牌")
                    return

            self.active_websockets.add(websocket)

            if platform != "unknown":
                previous = self.platform_websockets.get(platform)
                if previous and previous is not websocket:
                    logger.warning(f"检测到平台 {platform} 已有连接，正在关闭旧连接以接受新连接")
                    try:
                        await previous.close(code=1000, reason="新的连接已建立")
                    except Exception:
                        logger.debug(
                            f"关闭平台 {platform} 旧连接时出现异常", exc_info=True
                        )
                    finally:
                        self._remove_websocket(previous, platform, force=True)

                self.platform_websockets[platform] = websocket
                logger.info(f"平台 {platform} WebSocket 已连接")
            else:
                logger.info("收到未标记平台的 WebSocket 连接")

            try:
                while True:
                    try:
                        message = await websocket.receive_json()
                    except ValueError as exc:
                        logger.warning(f"平台 {platform} 收到无法解析的 JSON: {exc}")
                        continue

                    if platform != "unknown":
                        current = self.platform_websockets.get(platform)
                        if current is not websocket:
                            self.platform_websockets[platform] = websocket

                    task = asyncio.create_task(self.process_message(message))
                    self.add_background_task(task)
            except WebSocketDisconnect:
                logger.info(f"平台 {platform} WebSocket 已断开")
                self._remove_websocket(websocket, platform, force=True)
            except ConnectionResetError:
                logger.warning(f"平台 {platform} WebSocket 连接被重置")
                self._remove_websocket(websocket, platform, force=True)
            except asyncio.CancelledError:
                logger.debug(f"平台 {platform} WebSocket 任务被取消")
                self._remove_websocket(websocket, platform, force=True)
            except Exception as exc:  # pylint: disable=broad-except
                if _looks_like_connection_error(exc):
                    logger.debug(f"平台 {platform} WebSocket 连接关闭: {exc}")
                else:
                    logger.exception(f"平台 {platform} WebSocket 处理异常")
                self._remove_websocket(websocket, platform, force=True)

    def _remove_websocket(
        self,
        websocket: WebSocket,
        platform: Optional[str],
        *,
        force: bool = False,
    ) -> None:
        state = getattr(websocket, "client_state", None)
        state_name = None
        if isinstance(state, WebSocketState):
            state_name = state.name
        elif state is not None:
            state_name = str(state)

        if not force and state == WebSocketState.CONNECTED:
            logger.debug(
                f"跳过移除 WebSocket (platform={platform}, ws_id={id(websocket)}) 因状态仍为 CONNECTED"
            )
            return

        logger.debug(
            f"移除 WebSocket (platform={platform}, ws_id={id(websocket)}, state={state_name}, force={force})"
        )

        self.active_websockets.discard(websocket)

        if platform and self.platform_websockets.get(platform) is websocket:
            del self.platform_websockets[platform]
            logger.debug(f"已移除平台 {platform} 的 WebSocket 映射")
            return

        for key, value in list(self.platform_websockets.items()):
            if value is websocket:
                del self.platform_websockets[key]
                logger.debug(f"移除了非直接映射的平台 {key} 的 WebSocket")
                break

    # ------------------------------------------------------------------
    # 安全与配置信息
    # ------------------------------------------------------------------
    async def verify_token(self, token: str) -> bool:
        if not self.enable_token:
            return True
        return token in self.valid_tokens

    def add_valid_token(self, token: str) -> None:
        self.valid_tokens.add(token)

    def remove_valid_token(self, token: str) -> None:
        self.valid_tokens.discard(token)

    def _validate_ssl_files(self) -> None:
        if self.ssl_certfile and not os.path.exists(self.ssl_certfile):
            logger.error(f"SSL 证书文件不存在: {self.ssl_certfile}")
            raise FileNotFoundError(f"SSL 证书文件不存在: {self.ssl_certfile}")
        if self.ssl_keyfile and not os.path.exists(self.ssl_keyfile):
            logger.error(f"SSL 密钥文件不存在: {self.ssl_keyfile}")
            raise FileNotFoundError(f"SSL 密钥文件不存在: {self.ssl_keyfile}")
        if self.ssl_certfile and self.ssl_keyfile:
            logger.info(
                f"启用 SSL 证书: certfile={self.ssl_certfile}, keyfile={self.ssl_keyfile}"
            )

    def _build_uvicorn_config(self) -> uvicorn.Config:
        config_kwargs = {
            "host": self.host,
            "port": self.port,
            "ssl_certfile": self.ssl_certfile,
            "ssl_keyfile": self.ssl_keyfile,
            "ws_max_size": self.max_message_size,
            "ws_ping_interval": self.heartbeat_interval,
            "ws_ping_timeout": self.heartbeat_timeout,
        }

        if self.enable_custom_uvicorn_logger:
            config_kwargs["log_config"] = get_uvicorn_log_config()
            configure_uvicorn_logging()

        return uvicorn.Config(self.app, **config_kwargs)

    # ------------------------------------------------------------------
    # 生命周期控制
    # ------------------------------------------------------------------
    async def start(self) -> None:
        global logger  # noqa: PLW0603 - 保持 log_utils 中的单例
        logger = get_logger()

        self._running = True

        if not self.own_app:
            logger.info("WebSocket 服务已挂载至外部 FastAPI 应用，仅注册路由")
            return

        self._validate_ssl_files()

        config = self._build_uvicorn_config()
        self.server = uvicorn.Server(config)

        try:
            await self.server.serve()
        except Exception:  # pylint: disable=broad-except
            logger.exception("WebSocket 服务器启动失败")
            raise
        finally:
            self.server = None
            self._running = False

    def run_sync(self) -> None:
        global logger  # noqa: PLW0603 - 保持 log_utils 中的单例
        logger = get_logger()

        self._running = True

        if not self.own_app:
            logger.info("WebSocket 服务已挂载至外部 FastAPI 应用，仅注册路由")
            return

        self._validate_ssl_files()

        config = self._build_uvicorn_config()
        server = uvicorn.Server(config)

        try:
            server.run()
        except Exception:  # pylint: disable=broad-except
            logger.exception("WebSocket 服务器运行失败")
            raise
        finally:
            self._running = False

    async def stop(self) -> None:
        if not self._running:
            return

        self._running = False

        for websocket in list(self.active_websockets):
            try:
                await websocket.close(code=1000, reason="服务器关闭")
            except Exception:
                logger.debug("关闭 WebSocket 连接时出现异常", exc_info=True)

        self.active_websockets.clear()
        self.platform_websockets.clear()

        await self.cleanup_tasks()

        if self.server is not None:
            try:
                if hasattr(self.server, "should_exit"):
                    self.server.should_exit = True
                if hasattr(self.server, "force_exit"):
                    self.server.force_exit = True
                if hasattr(self.server, "shutdown"):
                    await self.server.shutdown()
            except Exception:
                logger.warning("停止 WebSocket 服务器时出现异常", exc_info=True)
            finally:
                self.server = None

    # ------------------------------------------------------------------
    # 消息发送与广播
    # ------------------------------------------------------------------
    async def broadcast_message(self, message: Dict[str, Any]) -> None:
        disconnected: Set[WebSocket] = set()

        for websocket in list(self.active_websockets):
            if websocket.client_state != WebSocketState.CONNECTED:
                disconnected.add(websocket)
                continue

            try:
                await websocket.send_json(message)
            except (WebSocketDisconnect, ConnectionResetError):
                disconnected.add(websocket)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(f"广播消息失败: {exc}")
                if _looks_like_connection_error(exc):
                    disconnected.add(websocket)

        for websocket in disconnected:
            platform = None
            for key, value in self.platform_websockets.items():
                if value is websocket:
                    platform = key
                    break

            self.active_websockets.discard(websocket)
            if platform:
                del self.platform_websockets[platform]

    async def send_message(self, target: str, message: Dict[str, Any]) -> bool:
        logger.debug(
            f"准备向平台 {target} 发送消息，当前映射平台: {list(self.platform_websockets.keys())}"
        )
        websocket = self.platform_websockets.get(target)
        if not websocket:
            logger.warning(f"未找到目标平台: {target}")
            return False

        if websocket.client_state != WebSocketState.CONNECTED:
            logger.warning(f"平台 {target} 的 WebSocket 连接已关闭")
            self._remove_websocket(websocket, target, force=True)
            return False

        logger.debug(
            f"平台 {target} WebSocket 状态: "
            f"{websocket.client_state.name if isinstance(websocket.client_state, WebSocketState) else str(websocket.client_state)} "
            f"(ws_id={id(websocket)})"
        )

        try:
            await websocket.send_json(message)
            logger.debug(f"向平台 {target} 发送消息成功")
            return True
        except WebSocketDisconnect:
            logger.warning(f"平台 {target} WebSocket 连接断开")
        except ConnectionResetError:
            logger.warning(f"平台 {target} 连接被重置")
        except Exception as exc:  # pylint: disable=broad-except
            if _looks_like_connection_error(exc):
                logger.debug(f"平台 {target} 连接异常: {exc}")
            else:
                logger.exception(f"发送消息到平台 {target} 失败")
        self._remove_websocket(websocket, target, force=True)

        return False


class WebSocketClient(BaseConnection, ClientConnectionInterface):
    """基于 WebSocket 的客户端实现。"""

    def __init__(self) -> None:
        super().__init__()
        self.url: Optional[str] = None
        self.platform: Optional[str] = None
        self.token: Optional[str] = None
        self.ssl_verify: Optional[str] = None
        self.headers: Dict[str, str] = {}
        self.max_message_size: int = 104_857_600

        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.ws_connected: bool = False
        self.session: Optional[aiohttp.ClientSession] = None

        self.reconnect_interval: int = 2
        self.retry_count: int = 0

        self.heartbeat_interval: int = 20

        self._monitor_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # 配置与连接管理
    # ------------------------------------------------------------------
    async def configure(
        self,
        url: str,
        platform: str,
        *,
        token: Optional[str] = None,
        ssl_verify: Optional[str] = None,
        max_message_size: Optional[int] = None,
        heartbeat_interval: Optional[int] = None,
    ) -> None:
        self.url = url
        self.platform = platform
        self.token = token
        self.ssl_verify = ssl_verify

        if max_message_size is not None:
            self.max_message_size = max_message_size
        if heartbeat_interval is not None:
            self.heartbeat_interval = max(1, int(heartbeat_interval))

        self.headers = {"platform": platform}
        if token:
            self.headers["Authorization"] = str(token)

    async def connect(self) -> bool:
        global logger  # noqa: PLW0603 - 保持 log_utils 中的单例
        logger = get_logger()

        if not self.url or not self.platform:
            raise ValueError("连接前必须先调用 configure 方法配置连接参数")

        ssl_context = None
        if self.url.startswith("wss://"):
            ssl_context = ssl.create_default_context()
            if self.ssl_verify:
                logger.info(f"使用证书验证: {self.ssl_verify}")
                ssl_context.load_verify_locations(self.ssl_verify)
            else:
                logger.warning("未提供证书验证，当前连接将跳过 SSL 校验")
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

        try:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            connector = aiohttp.TCPConnector(
                ssl=ssl_context, enable_cleanup_closed=True
            )

            if self.session and not self.session.closed:
                await self.session.close()

            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers=self.headers,
            )

            ws_kwargs = {
                "heartbeat": self.heartbeat_interval,
                "autoping": True,
                "compress": 15,
                "autoclose": True,
                "max_msg_size": self.max_message_size,
            }

            logger.info(f"正在连接到 {self.url}")
            self.ws = await self.session.ws_connect(self.url, **ws_kwargs)

            self.ws_connected = True
            self.retry_count = 0
            logger.info(f"已成功连接到 {self.url}")
            return True

        except aiohttp.ClientConnectorError as exc:
            logger.error(f"无法建立连接: {exc}")
        except aiohttp.ClientSSLError as exc:
            logger.error(f"SSL 错误: {exc}")
        except aiohttp.ClientError as exc:
            logger.error(f"连接错误: {exc}")
        except Exception:  # pylint: disable=broad-except
            logger.exception("连接时发生未预期的错误")
        finally:
            if not self.ws_connected:
                await self._cleanup_session()

        return False

    async def _cleanup_session(self) -> None:
        if self.session and not self.session.closed:
            try:
                await self.session.close()
                await asyncio.sleep(0.05)
            except Exception:
                logger.debug("关闭 ClientSession 时出现异常", exc_info=True)
            finally:
                self.session = None

    async def _cleanup_connection(self) -> None:
        try:
            if self.ws and not self.ws.closed:
                await self.ws.close()
        except Exception:
            logger.debug("关闭 WebSocket 时出现异常", exc_info=True)
        finally:
            self.ws = None

        await self._cleanup_session()
        self.ws_connected = False

    # ------------------------------------------------------------------
    # 生命周期控制
    # ------------------------------------------------------------------
    async def start(self) -> None:
        global logger  # noqa: PLW0603 - 保持 log_utils 中的单例
        logger = get_logger()

        if not self.ws_connected:
            await self.connect()

        self._running = True

        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._connection_monitor())
            self.add_background_task(self._monitor_task)

        while self._running:
            try:
                if not self.ws_connected or not self.ws:
                    success = await self.connect()
                    if not success:
                        retry_delay = min(
                            10, self.reconnect_interval * (2 ** min(self.retry_count, 5))
                        )
                        logger.info(f"等待 {retry_delay} 秒后重试...")
                        await asyncio.sleep(retry_delay)
                        self.retry_count += 1
                        continue

                async for msg in self.ws:
                    if not self._running:
                        break

                    if msg.type == WSMsgType.TEXT:
                        try:
                            data = msg.json()
                        except ValueError as exc:
                            logger.warning(f"无法解析服务器消息: {exc}")
                            continue

                        task = asyncio.create_task(self.process_message(data))
                        self.add_background_task(task)
                    elif msg.type == WSMsgType.BINARY:
                        logger.debug("收到二进制消息，已忽略")
                    elif msg.type == WSMsgType.PING:
                        logger.debug("收到服务器 PING，自动回复 PONG")
                    elif msg.type == WSMsgType.PONG:
                        logger.debug("收到服务器 PONG 响应")
                    elif msg.type == WSMsgType.ERROR:
                        logger.error(f"WebSocket 连接错误: {self.ws.exception()}")
                        self.ws_connected = False
                        break
                    elif msg.type == WSMsgType.CLOSED:
                        logger.warning("WebSocket 连接已关闭")
                        self.ws_connected = False
                        break

                self.ws_connected = False

            except asyncio.CancelledError:
                logger.debug("WebSocket 客户端任务被取消")
                break
            except KeyboardInterrupt:
                logger.debug("WebSocket 客户端因用户中断而关闭")
                self.ws_connected = False
                break
            except aiohttp.ServerTimeoutError:
                logger.warning("WebSocket 心跳超时，尝试重连")
                self.ws_connected = False
                self.retry_count += 1
            except Exception as exc:  # pylint: disable=broad-except
                if _looks_like_connection_error(exc):
                    logger.debug(f"WebSocket 连接关闭: {exc}")
                else:
                    logger.exception("WebSocket 连接发生错误")
                self.ws_connected = False
                self.retry_count += 1
                await self._cleanup_connection()
            finally:
                if self.ws and (self.ws.closed or self.ws.exception()):
                    self.ws_connected = False

            if self._running and not self.ws_connected:
                retry_delay = min(
                    10, self.reconnect_interval * (2 ** min(self.retry_count, 5))
                )
                logger.info(f"等待 {retry_delay} 秒后重试...")
                await asyncio.sleep(retry_delay)

    async def stop(self) -> None:
        logger.info("正在停止 WebSocket 客户端...")
        self._running = False

        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
                logger.debug("WebSocket 连接已关闭")
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(f"关闭 WebSocket 时出现异常: {exc}")
            finally:
                self.ws = None

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        await self.cleanup_tasks()

        await self._cleanup_session()

        self.ws_connected = False
        self._monitor_task = None
        logger.info("WebSocket 客户端已停止")

    async def send_message(self, target: str, message: Dict[str, Any]) -> bool:
        if not self.is_connected():
            logger.warning("WebSocket 未连接，无法发送消息")
            return False

        try:
            await self.ws.send_json(message)  # type: ignore[union-attr]
            return True
        except ConnectionResetError:
            logger.warning("连接被重置，标记为断开")
        except Exception as exc:  # pylint: disable=broad-except
            logger.error(f"发送消息失败: {exc}")

        self.ws_connected = False
        return False

    def is_connected(self) -> bool:
        if not self.ws_connected:
            return False

        if not self.ws:
            self.ws_connected = False
            return False

        if self.ws.closed:
            self.ws_connected = False
            return False

        if self.ws.exception():
            self.ws_connected = False
            return False

        return True

    async def ping(self) -> bool:
        if not self.is_connected():
            return False

        try:
            await self.ws.ping()  # type: ignore[union-attr]
            return True
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(f"Ping 失败: {exc}")
            self.ws_connected = False
            return False

    async def reconnect(self) -> bool:
        logger.info("尝试重新连接 WebSocket...")
        await self._cleanup_connection()
        self.retry_count = 0
        return await self.connect()

    async def _connection_monitor(self) -> None:
        interval = max(10, self.heartbeat_interval)
        while self._running:
            try:
                await asyncio.sleep(interval)

                if not self._running:
                    break

                if self.ws_connected and self.ws:
                    if self.ws.closed or self.ws.exception():
                        logger.warning("检测到连接异常，标记为断开")
                        self.ws_connected = False
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pylint: disable=broad-except
                if _looks_like_connection_error(exc):
                    logger.debug(f"连接监控任务出现异常: {exc}")
                else:
                    logger.error(f"连接监控任务出错: {exc}")
