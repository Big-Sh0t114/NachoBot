from src.logger import logger
from src.config import global_config
import time
import asyncio

from . import MetaEventType


class MetaEventHandler:
    """
    处理Meta事件
    """

    def __init__(self):
        self.interval = global_config.napcat_server.heartbeat_interval
        self._interval_checking = False

    async def handle_meta_event(self, message: dict) -> None:
        event_type = message.get("meta_event_type")
        if event_type == MetaEventType.lifecycle:
            sub_type = message.get("sub_type")
            if sub_type == MetaEventType.Lifecycle.connect:
                self_id = message.get("self_id")
                self.last_heart_beat = time.time()
                logger.success(f"Bot {self_id} 连接成功")
                asyncio.create_task(self.check_heartbeat(self_id))
        elif event_type == MetaEventType.heartbeat:
            self_id = message.get("self_id")
            if message["status"].get("online") and message["status"].get("good"):
                if not self._interval_checking:
                    asyncio.create_task(self.check_heartbeat(self_id))
                self.last_heart_beat = time.time()
                self.interval = message.get("interval") / 1000
            else:
                logger.warning(f"Bot {self_id} Napcat 端异常！")

    async def check_heartbeat(self, id: int) -> None:
        self._interval_checking = True
        try:
            while True:
                now_time = time.time()
                if now_time - self.last_heart_beat > self.interval * 2:
                    logger.error(f"Bot {id} 可能发生了连接断开，被下线，或者Napcat卡死！")
                    break
                else:
                    logger.debug("心跳正常")
                await asyncio.sleep(self.interval)
        finally:
            self._interval_checking = False


meta_event_handler = MetaEventHandler()
