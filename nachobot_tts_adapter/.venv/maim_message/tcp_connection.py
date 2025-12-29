"""
TCP通信实现模块，提供基于TCP的服务器和客户端实现
"""

import asyncio
import struct
import traceback
from typing import Dict, Any, Callable, List, Set, Optional, Tuple

from .connection_interface import (
    ServerConnectionInterface,
    ClientConnectionInterface,
    BaseConnection,
)
from .crypto import CryptoManager, FrameType
from .log_utils import get_logger

logger = get_logger()


class TCPConnection:
    """连接类，用于存储连接信息"""

    def __init__(self, writer: asyncio.StreamWriter, crypto_manager: CryptoManager):
        self.writer = writer
        self.crypto_manager = crypto_manager
        self.sequence = 0

    def get_sequence(self):
        """获取并递增序列号"""
        seq = self.sequence
        self.sequence += 1
        return seq


class BaseTCPConnection(BaseConnection):
    """TCP连接基类，提供基本的加密通信功能"""

    def __init__(self):
        super().__init__()
        self.crypto = CryptoManager()
        self._sequence = 0
        # 获取最新的logger实例
        global logger
        logger = get_logger()

    async def _read_frame(self, reader: asyncio.StreamReader) -> Tuple[int, bytes]:
        """读取一个完整的消息帧"""
        # 读取帧头（4字节长度 + 1字节类型）
        header = await reader.readexactly(5)
        frame_length, frame_type = struct.unpack("!IB", header)

        # 读取帧负载
        payload = await reader.readexactly(frame_length - 5)
        return frame_type, payload

    async def _write_frame(self, writer: asyncio.StreamWriter, frame: bytes):
        """写入一个消息帧"""
        writer.write(frame)
        await writer.drain()

    async def _handle_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        is_server: bool,
        crypto_manager: Optional[CryptoManager] = None,
    ) -> bool:
        """处理握手过程"""
        crypto: CryptoManager = crypto_manager or self.crypto
        try:
            if is_server:
                # 服务器等待客户端的握手请求
                frame_type, client_public_key = await self._read_frame(reader)
                # print(f"握手请求: {frame_type}")
                if frame_type != FrameType.HANDSHAKE_REQUEST:
                    logger.error("握手请求类型错误")
                    return False

                # 发送服务器的公钥
                server_handshake = crypto.create_handshake_frame(
                    crypto.get_public_bytes()
                )
                await self._write_frame(writer, server_handshake)

                # 计算共享密钥
                crypto.compute_shared_key(client_public_key)
            else:
                # 客户端发送握手请求
                client_handshake = crypto.create_handshake_frame(
                    crypto.get_public_bytes()
                )
                await self._write_frame(writer, client_handshake)

                # 等待服务器响应
                frame_type, server_public_key = await self._read_frame(reader)
                # print(f"握手响应: {frame_type}")
                if frame_type != FrameType.HANDSHAKE_REQUEST:
                    logger.error("握手请求类型错误")
                    return False

                # 计算共享密钥
                crypto.compute_shared_key(server_public_key)
            return True

        except Exception as e:
            logger.error(f"握手失败: {e}")
            logger.debug(traceback.format_exc())
            return False


class TCPServerConnection(BaseTCPConnection, ServerConnectionInterface):
    """TCP服务器实现"""

    def __init__(
        self, host: str = "0.0.0.0", port: int = 18000, enable_token: bool = False
    ):
        super().__init__()
        self.host = host
        self.port = port
        self.active_connections: Set[asyncio.StreamWriter] = set()
        self.platform_connections: Dict[str, TCPConnection] = {}  # 平台到连接的映射
        self._server = None
        self._serve_task = None

        # 令牌验证
        self.enable_token = enable_token
        self.valid_tokens: Set[str] = set()

    async def start(self):
        """启动服务器"""
        # 获取最新的logger引用
        global logger
        logger = get_logger()

        self._running = True
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info(f"TCP服务器启动在 {self.host}:{self.port}")

        async with self._server:
            self._serve_task = asyncio.create_task(self._server.serve_forever())
            try:
                await self._serve_task
            except (KeyboardInterrupt, asyncio.CancelledError):
                logger.info("TCP服务器收到停止信号")

    async def stop(self):
        """停止服务器"""
        if not self._running:
            return
        self._running = False

        # 取消服务任务
        if self._serve_task:
            self._serve_task.cancel()
            try:
                await self._serve_task
            except asyncio.CancelledError:
                pass

        # 关闭服务器
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # 关闭所有连接
        for writer in list(self.active_connections):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        self.active_connections.clear()
        self.platform_connections.clear()

        # 清理后台任务
        await self.cleanup_tasks()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """处理客户端连接"""
        logger.debug(f"新客户端连接: {writer.get_extra_info('peername')}")
        platform = None
        # 为每个连接创建独立的加密管理器
        connection_crypto = CryptoManager()

        try:
            # 进行握手(使用连接特定的加密管理器)
            if not await self._handle_handshake(
                reader, writer, True, crypto_manager=connection_crypto
            ):
                writer.close()
                await writer.wait_closed()
                logger.error("握手失败，关闭连接")
                return

            # 等待平台注册
            platform = await self._handle_platform_registration(
                reader, crypto_manager=connection_crypto
            )
            if not platform:
                logger.error("客户端未提供有效的平台信息")
                writer.close()
                await writer.wait_closed()
                return

            # 记录连接
            self.active_connections.add(writer)
            self.platform_connections[platform] = TCPConnection(
                writer=writer, crypto_manager=connection_crypto
            )
            logger.info(f"客户端已连接，平台: {platform}")

            try:
                # 持续接收消息
                while self._running:
                    try:
                        frame_type, payload = await self._read_frame(reader)
                        await self._process_message(
                            frame_type, payload, writer, connection_crypto
                        )
                    except asyncio.CancelledError:
                        logger.info(f"处理客户端连接的任务被取消 (平台: {platform})")
                        break
            except asyncio.IncompleteReadError:
                logger.info(f"客户端断开连接 (平台: {platform})")
            finally:
                await self._remove_connection(platform)

        except KeyboardInterrupt:
            logger.info("服务器关闭")
            raise
        except Exception as e:
            logger.error(f"处理客户端连接时出错 (平台: {platform}): {e}")
            logger.debug(traceback.format_exc())
            if platform:
                await self._remove_connection(platform)
            else:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _handle_platform_registration(
        self, reader: asyncio.StreamReader, crypto_manager: CryptoManager
    ) -> Optional[str]:
        """处理平台注册"""
        # 等待客户端发送平台信息
        frame_type, payload = await self._read_frame(reader)
        if frame_type != FrameType.DATA:
            return None

        # 解密并验证平台信息
        iv, encrypted_data, sequence = crypto_manager.parse_data_frame(payload)
        platform_data = crypto_manager.decrypt_message(iv, encrypted_data, sequence)

        if not isinstance(platform_data, dict) or "platform" not in platform_data:
            return None

        # 验证令牌
        if self.enable_token:
            if "token" not in platform_data or not await self.verify_token(
                platform_data["token"]
            ):
                logger.warning(f"平台 {platform_data['platform']} 的令牌验证失败")
                return None
            logger.debug(f"平台 {platform_data['platform']} 的令牌验证成功")

        return platform_data["platform"]

    async def _process_message(
        self,
        frame_type: FrameType,
        payload: bytes,
        writer: asyncio.StreamWriter,
        crypto_manager: CryptoManager,
    ):
        """异步处理单条消息"""
        try:
            if frame_type == FrameType.DATA:
                # 解析并处理数据帧
                iv, encrypted_data, sequence = crypto_manager.parse_data_frame(payload)
                message = crypto_manager.decrypt_message(iv, encrypted_data, sequence)

                # 处理消息
                task = asyncio.create_task(self.process_message(message))
                self.add_background_task(task)

            elif frame_type == FrameType.HEARTBEAT:
                # 回复心跳
                await self._write_frame(
                    writer, crypto_manager.create_frame(FrameType.HEARTBEAT, b"")
                )

        except Exception as e:
            logger.error(f"处理消息时出错: {e}")

    async def _remove_connection(self, platform: str):
        """移除连接"""
        if not platform or platform not in self.platform_connections:
            return

        conn = self.platform_connections.get(platform)
        if conn:
            writer = conn.writer
            if writer in self.active_connections:
                self.active_connections.remove(writer)

            del self.platform_connections[platform]

            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

            logger.info(f"平台 {platform} 的连接已关闭")

    async def broadcast_message(self, message: Dict[str, Any]):
        """广播消息给所有连接的客户端"""
        if not self.platform_connections:
            return

        # 发送给所有客户端
        disconnected = set()
        for platform, conn in self.platform_connections.items():
            try:
                sequence = conn.get_sequence()
                iv, encrypted = conn.crypto_manager.encrypt_message(message, sequence)
                frame = conn.crypto_manager.create_data_frame(iv, encrypted, sequence)
                await self._write_frame(conn.writer, frame)
            except Exception:
                disconnected.add(platform)

        # 清理断开的连接
        for platform in disconnected:
            await self._remove_connection(platform)

    async def send_message(self, target: str, message: Dict[str, Any]) -> bool:
        """发送消息给指定平台"""
        if target not in self.platform_connections:
            logger.warning(f"未找到目标平台: {target}")
            return False

        conn = self.platform_connections[target]
        try:
            # 使用连接特定的加密管理器加密消息
            sequence = conn.get_sequence()
            iv, encrypted = conn.crypto_manager.encrypt_message(message, sequence)
            frame = conn.crypto_manager.create_data_frame(iv, encrypted, sequence)

            # 发送消息
            await self._write_frame(conn.writer, frame)
            return True
        except Exception as e:
            logger.error(f"发送消息到平台 {target} 失败: {e}")
            await self._remove_connection(target)
            return False

    async def verify_token(self, token: str) -> bool:
        """验证令牌是否有效"""
        if not self.enable_token:
            return True
        # logger.debug(f"验证令牌: {token} in {self.valid_tokens}")
        return token in self.valid_tokens

    def add_valid_token(self, token: str):
        """添加有效令牌"""
        self.valid_tokens.add(token)

    def remove_valid_token(self, token: str):
        """移除有效令牌"""
        self.valid_tokens.discard(token)


class TCPClientConnection(BaseTCPConnection, ClientConnectionInterface):
    """TCP客户端实现"""

    def __init__(self, platform: str, token: Optional[str] = None):
        super().__init__()
        self.platform = platform
        self.token = token
        self.host = None
        self.port = None
        self.reader = None
        self.writer = None
        self._heartbeat_task = None

    def set_target(self, host: str, port: int):
        """设置目标服务器地址和端口"""
        self.host = host
        self.port = port

    async def connect(self) -> bool:
        """连接到服务器"""
        # 获取最新的logger引用
        global logger
        logger = get_logger()

        if not self.host or not self.port:
            logger.error("连接前必须先调用set_target方法设置目标服务器")
            return False

        host, port = self.host, self.port

        try:
            logger.info(f"正在连接到TCP服务器 {host}:{port}")
            self.reader, self.writer = await asyncio.open_connection(host, port)

            # 进行握手
            if not await self._handle_handshake(self.reader, self.writer, False):
                logger.error("握手失败")
                self.writer.close()
                await self.writer.wait_closed()
                self.reader = None
                self.writer = None
                return False

            # 发送平台信息和令牌(如果有)
            platform_message = {"platform": self.platform}
            if self.token:
                platform_message["token"] = self.token

            iv, encrypted = self.crypto.encrypt_message(
                platform_message, self._sequence
            )
            frame = self.crypto.create_data_frame(iv, encrypted, self._sequence)
            self._sequence += 1

            await self._write_frame(self.writer, frame)

            # 启动心跳任务
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._heartbeat())
                self.add_background_task(self._heartbeat_task)

            logger.info(f"已连接到TCP服务器 {host}:{port}")
            return True

        except ConnectionRefusedError:
            logger.error(f"连接被拒绝: {host}:{port}")
            return False
        except Exception as e:
            logger.error(f"连接失败: {e}")
            return False

    async def start(self):
        """开始接收消息循环"""
        # 获取最新的logger引用
        global logger
        logger = get_logger()

        if not self.writer:
            success = await self.connect()
            if not success:
                return

        self._running = True

        try:
            while self._running:
                try:
                    # 读取消息
                    frame_type, payload = await self._read_frame(self.reader)

                    if frame_type == FrameType.DATA:
                        # 解析并处理数据帧
                        iv, encrypted_data, sequence = self.crypto.parse_data_frame(
                            payload
                        )
                        message = self.crypto.decrypt_message(
                            iv, encrypted_data, sequence
                        )

                        # 处理消息
                        task = asyncio.create_task(self.process_message(message))
                        self.add_background_task(task)

                except asyncio.IncompleteReadError:
                    logger.info("与服务器的连接已断开")
                    break
                except asyncio.CancelledError:
                    logger.info("接收消息循环被取消")
                    break
                except Exception as e:
                    logger.error(f"接收或处理消息时错误: {e}")
                    logger.debug(traceback.format_exc())
                    break

            # 如果连接断开了但仍在运行，尝试重新连接
            if self._running:
                self.writer.close()
                try:
                    await self.writer.wait_closed()
                except Exception:
                    pass
                self.writer = None
                self.reader = None

                # 等待一段时间后重新连接
                await asyncio.sleep(5)
                await self.connect()

        except asyncio.CancelledError:
            logger.info("客户端任务被取消")
            raise

    async def stop(self):
        """停止客户端"""
        self._running = False

        # 取消心跳任务
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

        # 关闭连接
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
            self.reader = None

        # 清理后台任务
        await self.cleanup_tasks()

    async def send_message(self, target: str, message: Dict[str, Any]) -> bool:
        """发送消息到服务器"""
        if not self.writer or self.writer.is_closing():
            await self.connect()
            if not self.writer:
                return False

        # 加密并发送消息
        try:
            iv, encrypted = self.crypto.encrypt_message(message, self._sequence)
            frame = self.crypto.create_data_frame(iv, encrypted, self._sequence)
            self._sequence += 1

            await self._write_frame(self.writer, frame)
            return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False

    async def _heartbeat(self):
        """发送心跳包"""
        # 更新logger引用
        global logger
        logger = get_logger()

        while self._running:
            try:
                await asyncio.sleep(15)  # 每15秒发送一次心跳
                if self.writer and not self.writer.is_closing():
                    await self._write_frame(
                        self.writer, self.crypto.create_frame(FrameType.HEARTBEAT, b"")
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"发送心跳时出错: {e}")
                # 如果发送心跳失败，可能连接已断开，尝试重新连接
                if self._running:
                    try:
                        await self.connect()
                    except Exception:
                        await asyncio.sleep(5)  # 连接失败后等待5秒再重试

    def is_connected(self) -> bool:
        """
        判断当前连接是否有效（存活）

        Returns:
            bool: 连接是否有效
        """
        return (
            self.writer is not None
            and self.reader is not None
            and not self.writer.is_closing()
        )
