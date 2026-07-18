import asyncio
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

    def __init__(self):
        self.heartflow_chat_list: Dict[Any, HeartFChatting | BrainChatting] = {}
        self._registry_lock = asyncio.Lock()
        self._creation_tasks: Dict[Any, asyncio.Task[HeartFChatting | BrainChatting]] = {}
        self._shutting_down = False

    async def get_or_create_heartflow_chat(self, chat_id: Any) -> Optional[HeartFChatting | BrainChatting]:
        """获取或创建一个新的HeartFChatting实例"""
        try:
            async with self._registry_lock:
                if self._shutting_down:
                    return None
                if chat := self.heartflow_chat_list.get(chat_id):
                    return chat
                creation_task = self._creation_tasks.get(chat_id)
                if creation_task is None:
                    creation_task = asyncio.create_task(self._create_heartflow_chat(chat_id))
                    self._creation_tasks[chat_id] = creation_task

            try:
                return await asyncio.shield(creation_task)
            finally:
                if creation_task.done():
                    async with self._registry_lock:
                        if self._creation_tasks.get(chat_id) is creation_task:
                            self._creation_tasks.pop(chat_id, None)
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
        await new_chat.start()
        async with self._registry_lock:
            if self._shutting_down:
                should_stop = True
            else:
                self.heartflow_chat_list[chat_id] = new_chat
                should_stop = False
        if should_stop:
            await new_chat.stop()
            raise asyncio.CancelledError("Heartflow 正在关闭")
        return new_chat

    async def stop_chat(self, chat_id: Any) -> None:
        """停止并移除单个聊天运行时。"""
        async with self._registry_lock:
            chat = self.heartflow_chat_list.pop(chat_id, None)
        if chat is not None:
            await chat.stop()

    async def stop_all(self) -> None:
        """先阻止新建，再停止全部运行时；不会在持锁时等待外部协程。"""
        async with self._registry_lock:
            self._shutting_down = True
            chats = list(self.heartflow_chat_list.values())
            self.heartflow_chat_list.clear()
            creation_tasks = list(self._creation_tasks.values())
            self._creation_tasks.clear()
        for task in creation_tasks:
            task.cancel()
        if creation_tasks:
            await asyncio.gather(*creation_tasks, return_exceptions=True)
        if chats:
            await asyncio.gather(*(chat.stop() for chat in chats), return_exceptions=True)


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
