import asyncio
import itertools
import json
import logging
import time
import uuid
from typing import Any, Dict, Tuple

from ncnk_message import (
    BaseMessageInfo,
    FormatInfo,
    GroupInfo,
    MessageBase,
    Seg,
    UserInfo,
)

ACCEPT_FORMAT = ["text", "voice", "reply", "command"]

class EventManager:
    def __init__(self, config: Any, logger: logging.Logger, adapter_ref: Any):
        self.config = config
        self.logger = logger
        self.adapter = adapter_ref
        
        self.event_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.seq_counter = itertools.count()
        
        self.gift_buffer: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
        self.last_gift_time: Dict[Tuple[int, str, str], float] = {}

    def push_to_event_queue(self, priority: int, message: MessageBase) -> None:
        """Push an event to the priority queue with a sequence number to preserve order."""
        seq = next(self.seq_counter)
        self.event_queue.put_nowait((priority, time.time(), seq, message))

    async def event_consumer_loop(self) -> None:
        """Background loop to consume events and send to Core sequentially."""
        self.logger.info("Event serialization queue started.")
        while True:
            try:
                priority, timestamp, count, message = await self.event_queue.get()
                try:
                    await self.adapter._send_to_nachobot(message)
                except Exception as e:
                    self.logger.error(f"Failed to send event to core: {e}")
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Event processing loop error: {e}")
                await asyncio.sleep(1)

    async def gift_flush_loop(self) -> None:
        """Check gift buffer for idle streams and flush them."""
        self.logger.info("Gift aggregation loop started.")
        debounce_seconds = 2.0
        while True:
            try:
                await asyncio.sleep(1.0)
                now = time.time()
                keys_to_flush = []

                for key, last_ts in list(self.last_gift_time.items()):
                    if now - last_ts >= debounce_seconds:
                        keys_to_flush.append(key)

                for key in keys_to_flush:
                    if key in self.gift_buffer:
                        data = self.gift_buffer.pop(key)
                        del self.last_gift_time[key]

                        count = data["count"]
                        room_id, user_id, gift_name = key
                        user_name = data["user_name"]
                        timestamp = data["timestamp"]
                        price = data["price"] * count

                        self.logger.info(f"Flushing aggregated gift: {gift_name} x{count} from {user_name}")

                        prompt_text = f"送出了 {gift_name} x{count}"
                        template_info = await self.adapter._get_template_info(room_id, user_id, prompt_text)
                        
                        additional_config = {"is_mentioned": 1.0}
                        
                        message_info = BaseMessageInfo(
                            platform=self.config.platform,
                            message_id=str(uuid.uuid4()),
                            time=timestamp,
                            user_info=UserInfo(
                                platform=self.config.platform,
                                user_id=user_id,
                                user_nickname=user_name,
                            ),
                            group_info=GroupInfo(
                                platform=self.config.platform,
                                group_id=str(room_id),
                                group_name=str(room_id),
                            ),
                            format_info=FormatInfo(
                                content_format=["text"],
                                accept_format=ACCEPT_FORMAT,
                            ),
                            template_info=template_info,
                            additional_config=additional_config,
                        )

                        gift_segment = Seg(type="gift", data=f"{gift_name}:{count}")
                        text_segment = Seg(type="text", data=prompt_text)

                        message = MessageBase(
                            message_info=message_info,
                            message_segment=Seg(
                                type="seglist", data=[gift_segment, text_segment]
                            ),
                            raw_message=json.dumps(
                                {
                                    "type": "gift",
                                    "gift_name": gift_name,
                                    "num": count,
                                    "price": price,
                                    "room_id": room_id,
                                },
                                ensure_ascii=True,
                            ),
                        )

                        asyncio.create_task(self.adapter._send_to_nachobot(message))

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Gift flush loop error: {e}")
                await asyncio.sleep(1)
