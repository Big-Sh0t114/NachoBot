"""
中期记忆后台摘要构建器
独立的 asyncio 后台循环，定期扫描活跃 chat_id 并生成摘要。
与 observe/planner 主流程完全解耦，不阻塞任何用户响应。
"""

import asyncio
import time
from typing import Dict, Optional, Set

from src.common.logger import get_logger
from src.memory_system.mid_term_memory_config import get_mid_term_memory_config
from src.memory_system.mid_term_memory import get_mid_term_memory_manager
from src.plugin_system.apis import message_api

logger = get_logger("mid_term_memory_builder")


class MidTermMemoryBuilder:
    """
    后台摘要构建器

    - 每隔 check_interval 秒扫描一次所有已注册的活跃 chat_id
    - 对每个 chat_id 调用 maybe_build_summary
    - 完全异步，不阻塞主流程
    """

    def __init__(self, check_interval: float = 60.0):
        """
        Args:
            check_interval: 后台循环检查间隔（秒）
        """
        self._config = get_mid_term_memory_config()
        self._check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # 已注册的活跃 chat_id -> 最近一次消息时间戳
        self._active_chats: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def start(self):
        """启动后台循环"""
        if self._running:
            logger.debug("中期记忆后台构建器已在运行")
            return

        if not self._config.enabled:
            logger.info("中期记忆已禁用，后台构建器不启动")
            return

        self._running = True
        self._task = asyncio.create_task(self._build_loop())
        logger.info(
            f"中期记忆后台构建器已启动 | 检查间隔: {self._check_interval}s"
        )

    async def stop(self):
        """停止后台循环"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("中期记忆后台构建器已停止")

    def register_chat(self, chat_id: str, latest_msg_time: Optional[float] = None):
        """
        注册一个活跃的 chat_id（由 heartFC_chat 在启动时或收到消息时调用）

        Args:
            chat_id: 聊天 ID
            latest_msg_time: 最新消息时间（用于判断窗口滑动）
        """
        self._active_chats[chat_id] = latest_msg_time or time.time()

    def unregister_chat(self, chat_id: str):
        """注销一个 chat_id（聊天流关闭时调用）"""
        self._active_chats.pop(chat_id, None)

    def update_chat_time(self, chat_id: str, msg_time: float):
        """更新 chat_id 的最新消息时间"""
        self._active_chats[chat_id] = msg_time

    async def _build_loop(self):
        """后台主循环"""
        try:
            # 启动后等待一小段时间，让系统完成初始化
            await asyncio.sleep(10.0)

            while self._running:
                try:
                    await self._scan_and_build()
                except Exception as e:
                    logger.error(f"中期记忆后台构建循环出错: {e}", exc_info=True)

                await asyncio.sleep(self._check_interval)

        except asyncio.CancelledError:
            logger.debug("中期记忆后台构建循环被取消")
            raise

    async def _scan_and_build(self):
        """扫描所有活跃 chat_id，尝试生成摘要"""
        if not self._active_chats:
            return

        # 复制一份避免迭代时修改
        chats_snapshot = dict(self._active_chats)

        for chat_id, latest_time in chats_snapshot.items():
            if not self._running:
                break

            try:
                await self._build_for_chat(chat_id, latest_time)
            except Exception as e:
                logger.warning(
                    f"[{chat_id}] 后台摘要生成失败: {e}", exc_info=True
                )

            # 每个 chat 之间 yield 一下，避免长时间占用事件循环
            await asyncio.sleep(0.1)

    async def _build_for_chat(self, chat_id: str, latest_time: float):
        """为单个 chat_id 尝试生成摘要"""
        manager = get_mid_term_memory_manager(chat_id)

        # 获取当前上下文窗口最旧消息时间
        # 使用与 planner 相同的逻辑：取上下文窗口大小对应的消息列表的最旧时间
        try:
            from src.config.config import global_config

            is_group = True  # 保守估计用群聊上下文大小
            context_size = global_config.chat.get_max_context_size(is_group_chat=is_group)
            _size = int(context_size * 0.6)

            # 获取消息列表来确定窗口最旧时间
            from src.chat.utils.chat_message_builder import get_raw_msg_before_timestamp_with_chat

            messages = get_raw_msg_before_timestamp_with_chat(
                chat_id=chat_id,
                timestamp=time.time(),
                limit=_size,
            )

            if not messages:
                return

            oldest_msg_time = messages[0].time
            await manager.maybe_build_summary(
                current_window_oldest_time=oldest_msg_time,
            )

        except Exception as e:
            logger.debug(f"[{chat_id}] 后台获取窗口信息失败: {e}")


# 全局单例
_builder_instance: Optional[MidTermMemoryBuilder] = None


def get_mid_term_memory_builder() -> MidTermMemoryBuilder:
    """获取全局后台构建器实例"""
    global _builder_instance
    if _builder_instance is None:
        config = get_mid_term_memory_config()
        # 检查间隔设为冷却时间的一半，确保及时响应
        interval = max(30.0, config.min_summary_interval_seconds / 2)
        _builder_instance = MidTermMemoryBuilder(check_interval=interval)
    return _builder_instance
