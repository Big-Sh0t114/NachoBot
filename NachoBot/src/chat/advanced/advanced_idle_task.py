import time

from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.advanced.advanced_manager import advanced_manager
from src.chat.message_receive.chat_stream import get_chat_manager
from src.manager.async_task_manager import AsyncTask
from src.plugin_system.apis import send_api

logger = get_logger("advanced_idle_task")


class AdvancedIdleTimeoutTask(AsyncTask):
    """定期检查高级模式闲置并自动关闭"""

    def __init__(self):
        cfg = global_config.advanced
        timeout_minutes = max(1, int(getattr(cfg, "idle_timeout_minutes", 20) or 0))
        interval_seconds = max(10, int(getattr(cfg, "idle_check_interval_seconds", 60) or 0))
        super().__init__(
            task_name="Advanced Idle Timeout Task",
            wait_before_start=interval_seconds,
            run_interval=interval_seconds,
        )
        self._timeout_seconds = timeout_minutes * 60
        self._notice = getattr(cfg, "idle_notice", "20分钟未收到你的新消息，高级模式已自动关闭哦~")

    async def run(self):
        now = time.time()
        chat_manager = get_chat_manager()

        for stream in list(chat_manager.streams.values()):
            # 只检查私聊
            if stream.group_info:
                continue

            user_info = getattr(stream, "user_info", None)
            user_id = getattr(user_info, "user_id", None) if user_info else None
            if not user_id:
                continue

            if not advanced_manager.is_on(stream):
                continue

            idle_seconds = now - getattr(stream, "last_active_time", now)
            if idle_seconds < self._timeout_seconds:
                continue

            advanced_manager.set_state(str(user_id), False, stream_id=stream.stream_id)
            logger.info(
                f"[AdvancedIdle] user={user_id}, stream={stream.stream_id} idle_for={int(idle_seconds)}s -> turned off"
            )
            try:
                await send_api.text_to_stream(self._notice, stream.stream_id)
            except Exception as e:
                logger.error(f"[AdvancedIdle] notify failed for stream={stream.stream_id}: {e}", exc_info=True)
