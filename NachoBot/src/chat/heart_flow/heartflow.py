import asyncio
import time
import traceback
from typing import Any, Optional, Dict

from src.chat.message_receive.chat_stream import get_chat_manager
from src.common.logger import get_logger
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.message_receive.chat_stream import ChatStream

logger = get_logger("heartflow")


class Heartflow:
    """主心流协调器，负责初始化并协调聊天"""

    def __init__(
        self,
        *,
        idle_ttl_seconds: float = 30 * 60,
        cleanup_interval_seconds: float = 60,
        max_cached_chats: int = 256,
    ):
        self.heartflow_chat_list: Dict[Any, HeartFChatting | BrainChatting] = {}
        self._registry_lock = asyncio.Lock()
        self._creation_tasks: Dict[Any, asyncio.Task[HeartFChatting | BrainChatting]] = {}
        self._stopping_tasks: Dict[Any, asyncio.Task[None]] = {}
        self._last_accessed: Dict[Any, float] = {}
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        self.idle_ttl_seconds = max(0.0, idle_ttl_seconds)
        self.cleanup_interval_seconds = max(0.0, cleanup_interval_seconds)
        self.max_cached_chats = max(1, max_cached_chats)
        self._shutting_down = False

    async def get_or_create_heartflow_chat(self, chat_id: Any) -> Optional[HeartFChatting | BrainChatting]:
        """获取或创建一个新的HeartFChatting实例"""
        try:
            while True:
                creation_task = None
                stopping_task = None
                async with self._registry_lock:
                    if self._shutting_down:
                        return None
                    if chat := self.heartflow_chat_list.get(chat_id):
                        self._last_accessed[chat_id] = time.monotonic()
                        self._ensure_cleanup_task_locked()
                        result = chat
                    else:
                        result = None
                        stopping_task = self._stopping_tasks.get(chat_id)
                        if stopping_task is None:
                            creation_task = self._creation_tasks.get(chat_id)
                            if creation_task is None:
                                creation_task = asyncio.create_task(
                                    self._create_heartflow_chat(chat_id),
                                    name=f"heartflow-create-{chat_id}",
                                )
                                self._creation_tasks[chat_id] = creation_task
                            self._ensure_cleanup_task_locked()

                if result is not None:
                    await self._enforce_cache_limit(exclude_chat_id=chat_id)
                    return result
                if stopping_task is not None:
                    await asyncio.shield(stopping_task)
                    continue
                if creation_task is None:
                    continue
                try:
                    result = await asyncio.shield(creation_task)
                finally:
                    if creation_task.done():
                        async with self._registry_lock:
                            if self._creation_tasks.get(chat_id) is creation_task:
                                self._creation_tasks.pop(chat_id, None)
                await self._enforce_cache_limit(exclude_chat_id=chat_id)
                return result
        except Exception as e:
            logger.error(f"创建心流聊天 {chat_id} 失败: {e}", exc_info=True)
            traceback.print_exc()
            return None

    async def _create_heartflow_chat(self, chat_id: Any) -> HeartFChatting | BrainChatting:
        """实际创建聊天运行时；同一 chat_id 只会有一个任务进入这里。"""
        chat_stream: ChatStream | None = get_chat_manager().get_stream(chat_id)
        if not chat_stream:
            raise ValueError(f"未找到 chat_id={chat_id} 的聊天流")
        new_chat: HeartFChatting | BrainChatting
        if chat_stream.group_info:
            new_chat = HeartFChatting(chat_id=chat_id)
        else:
            new_chat = BrainChatting(chat_id=chat_id)
        try:
            await new_chat.start()
            async with self._registry_lock:
                if self._shutting_down:
                    should_stop = True
                else:
                    self.heartflow_chat_list[chat_id] = new_chat
                    self._last_accessed[chat_id] = time.monotonic()
                    should_stop = False
            if should_stop:
                await new_chat.stop()
                raise asyncio.CancelledError("Heartflow 正在关闭")
            return new_chat
        except BaseException:
            if getattr(new_chat, "running", False):
                await new_chat.stop()
            raise

    def _ensure_cleanup_task_locked(self) -> None:
        if self.cleanup_interval_seconds <= 0 or self._shutting_down:
            return
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(),
                name="heartflow-runtime-cleanup",
            )

    def _is_chat_protected(self, chat_id: Any, chat: HeartFChatting | BrainChatting) -> bool:
        """正在 Focus 管理或 Planner 执行中的聊天不参与淘汰。"""
        try:
            if focus_coordinator.is_managed(str(chat_id)):
                return True
        except Exception:
            logger.debug(f"检查聊天 {chat_id} 的 Focus 状态失败", exc_info=True)
        try:
            from src.chat.heart_flow.appointment_scheduler import appointment_scheduler

            if appointment_scheduler.get_pending(str(chat_id)):
                return True
        except Exception:
            logger.debug(f"检查聊天 {chat_id} 的预约任务失败", exc_info=True)
        return (
            getattr(chat, "_planner_interrupt_flag", None) is not None
            or getattr(chat, "_in_flight_operations", 0) > 0
        )

    async def _stop_runtime(self, chat_id: Any, chat: HeartFChatting | BrainChatting) -> None:
        try:
            await chat.stop()
        except Exception:
            logger.error(f"停止聊天运行时 {chat_id} 失败", exc_info=True)
        finally:
            current = asyncio.current_task()
            async with self._registry_lock:
                if self._stopping_tasks.get(chat_id) is current:
                    self._stopping_tasks.pop(chat_id, None)

    def _detach_chat_locked(
        self,
        chat_id: Any,
        *,
        expected_last_access: Optional[float] = None,
    ) -> Optional[asyncio.Task[None]]:
        existing_task = self._stopping_tasks.get(chat_id)
        if existing_task is not None:
            return existing_task
        if expected_last_access is not None and self._last_accessed.get(chat_id) != expected_last_access:
            return None
        chat = self.heartflow_chat_list.get(chat_id)
        if chat is None:
            self._last_accessed.pop(chat_id, None)
            return None
        if expected_last_access is not None and self._is_chat_protected(chat_id, chat):
            return None
        self.heartflow_chat_list.pop(chat_id, None)
        self._last_accessed.pop(chat_id, None)
        task = asyncio.create_task(
            self._stop_runtime(chat_id, chat),
            name=f"heartflow-stop-{chat_id}",
        )
        self._stopping_tasks[chat_id] = task
        return task

    async def stop_chat(self, chat_id: Any) -> None:
        """停止并移除单个聊天运行时。"""
        async with self._registry_lock:
            creation_task = self._creation_tasks.pop(chat_id, None)
            if creation_task is not None:
                creation_task.cancel()
            task = self._detach_chat_locked(chat_id)
        if creation_task is not None:
            await asyncio.gather(creation_task, return_exceptions=True)
        if task is not None:
            await asyncio.shield(task)

    async def cleanup_idle_chats(self) -> int:
        """停止超过 TTL 且没有 Focus/Planner 保护的运行时。"""
        if self.idle_ttl_seconds <= 0:
            return 0
        cutoff = time.monotonic() - self.idle_ttl_seconds
        async with self._registry_lock:
            candidates = [
                (chat_id, last_access)
                for chat_id, last_access in self._last_accessed.items()
                if last_access <= cutoff
            ]
        stopped = 0
        for chat_id, last_access in candidates:
            async with self._registry_lock:
                task = self._detach_chat_locked(chat_id, expected_last_access=last_access)
            if task is not None:
                await asyncio.shield(task)
                stopped += 1
        return stopped

    async def _enforce_cache_limit(self, exclude_chat_id: Any) -> None:
        async with self._registry_lock:
            excess = len(self.heartflow_chat_list) - self.max_cached_chats
            if excess <= 0:
                return
            candidates = sorted(
                (
                    (last_access, chat_id)
                    for chat_id, last_access in self._last_accessed.items()
                    if chat_id != exclude_chat_id
                ),
                key=lambda item: item[0],
            )
        stopped = 0
        for last_access, chat_id in candidates:
            async with self._registry_lock:
                task = self._detach_chat_locked(chat_id, expected_last_access=last_access)
            if task is not None:
                await asyncio.shield(task)
                stopped += 1
                if stopped >= excess:
                    break

    async def _cleanup_loop(self) -> None:
        try:
            while not self._shutting_down:
                await asyncio.sleep(self.cleanup_interval_seconds)
                try:
                    await self.cleanup_idle_chats()
                    await self._enforce_cache_limit(exclude_chat_id=None)
                except Exception:
                    logger.error("Heartflow 运行时清理失败，下一周期将重试", exc_info=True)
        except asyncio.CancelledError:
            raise

    async def stop_all(self) -> None:
        """先阻止新建，再停止全部运行时；不会在持锁时等待外部协程。"""
        async with self._registry_lock:
            self._shutting_down = True
            cleanup_task = self._cleanup_task
            self._cleanup_task = None
            creation_tasks = list(self._creation_tasks.values())
            self._creation_tasks.clear()
            for chat_id in tuple(self.heartflow_chat_list):
                self._detach_chat_locked(chat_id)
            stopping_tasks = list(self._stopping_tasks.values())
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        for task in creation_tasks:
            task.cancel()
        if creation_tasks:
            await asyncio.gather(*creation_tasks, return_exceptions=True)
        if stopping_tasks:
            await asyncio.gather(*stopping_tasks, return_exceptions=True)


heartflow = Heartflow()


async def _ensure_focus_runtime(chat_id: str) -> None:
    """Prepare a Focus switch target before committing its lease."""

    runtime = await heartflow.get_or_create_heartflow_chat(chat_id)
    if runtime is None:
        raise RuntimeError(f"Cannot create Focus target runtime: {chat_id}")


# Keep the coordinator independent from Heartflow while always installing the
# target-runtime preparation hook when Heartflow is imported.
from src.chat.focus.coordinator import focus_coordinator  # noqa: E402

focus_coordinator.set_ensure_runtime_callback(_ensure_focus_runtime)
