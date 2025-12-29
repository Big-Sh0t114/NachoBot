"""
连接接口模块，定义了消息系统中所有通信类的基本接口
"""

import asyncio
from typing import Dict, Any, Callable, List, Set, Optional
from abc import ABC, abstractmethod


class ConnectionInterface(ABC):
    """通信连接接口，定义所有通信类必须实现的方法"""

    @abstractmethod
    async def start(self):
        """启动连接/服务"""
        pass

    @abstractmethod
    async def stop(self):
        """停止连接/服务"""
        pass

    @abstractmethod
    async def send_message(self, target: str, message: Dict[str, Any]) -> bool:
        """
        发送消息

        Args:
            target: 目标标识符(如平台ID)
            message: 要发送的消息

        Returns:
            bool: 发送是否成功
        """
        pass

    @abstractmethod
    def register_message_handler(self, handler: Callable):
        """
        注册消息处理器

        Args:
            handler: 消息处理函数
        """
        pass


class ServerConnectionInterface(ConnectionInterface):
    """服务器连接接口，定义服务器特有的方法"""

    @abstractmethod
    async def broadcast_message(self, message: Dict[str, Any]):
        """
        广播消息给所有连接的客户端

        Args:
            message: 要广播的消息
        """
        pass


class ClientConnectionInterface(ConnectionInterface):
    """客户端连接接口，定义客户端特有的方法"""

    @abstractmethod
    async def connect(self) -> bool:
        """
        连接到服务器

        Returns:
            bool: 连接是否成功
        """
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """
        判断当前连接是否有效（存活）

        Returns:
            bool: 连接是否有效
        """
        pass


class BaseConnection(ABC):
    """连接基类，提供通用的连接管理功能"""

    def __init__(self):
        self.message_handlers: List[Callable] = []
        self.background_tasks: Set[asyncio.Task] = set()
        self._running = False

    def add_background_task(self, task: asyncio.Task):
        """添加后台任务到管理集合"""
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    def register_message_handler(self, handler: Callable):
        """注册消息处理器"""
        if handler not in self.message_handlers:
            self.message_handlers.append(handler)

    async def process_message(self, message: Dict[str, Any]):
        """处理单条消息"""
        tasks = []
        for handler in self.message_handlers:
            try:
                result = handler(message)
                if asyncio.iscoroutine(result):
                    task = asyncio.create_task(result)
                    tasks.append(task)
                    self.add_background_task(task)
            except Exception as e:
                import logging

                logging.error(f"处理消息时出错: {e}")
                import traceback

                logging.debug(traceback.format_exc())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cleanup_tasks(self):
        """清理所有后台任务"""
        # 取消所有后台任务
        for task in self.background_tasks:
            if not task.done():
                task.cancel()

        # 等待所有任务完成
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)

        self.background_tasks.clear()
