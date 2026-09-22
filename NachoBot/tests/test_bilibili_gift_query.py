import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from peewee import SqliteDatabase

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BILIBILI_ADAPTER_ROOT = PROJECT_ROOT / "NachoBot-Bilibili-Adapter"
if str(BILIBILI_ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(BILIBILI_ADAPTER_ROOT))

import bili_src.live.event_manager as event_manager_module
from bili_src.live.event_manager import EventManager
from src.chat.utils.chat_message_builder import get_raw_msg_by_timestamp_with_chat
from src.common.database.database_model import Messages


class BilibiliDebouncedGiftQueryTests(unittest.TestCase):
    def test_flushed_gift_is_visible_after_existing_non_focus_read_watermark(self):
        async def exercise(database: SqliteDatabase):
            read_watermark = 900.0
            occurrence_timestamp = read_watermark - 100.0
            stream_id = "bilibili.live_group_100"
            scheduled_tasks = []
            adapter = SimpleNamespace(
                _get_template_info=AsyncMock(return_value=None),
                _build_live_additional_config=Mock(return_value={"room_id": "100"}),
            )

            async def persist_emitted_message(message):
                info = message.message_info
                group_info = info.group_info
                Messages.create(
                    message_id=info.message_id,
                    time=info.time,
                    chat_id=stream_id,
                    chat_info_stream_id=stream_id,
                    chat_info_platform=info.platform,
                    chat_info_user_platform="",
                    chat_info_user_id="",
                    chat_info_user_nickname="",
                    chat_info_create_time=1.0,
                    chat_info_last_active_time=info.time,
                    chat_info_group_platform=group_info.platform,
                    chat_info_group_id=group_info.group_id,
                    chat_info_group_name=group_info.group_name,
                    user_platform=None,
                    user_id=None,
                    user_nickname=None,
                    user_cardname=None,
                    processed_plain_text="送出了 测试礼物 x2",
                    display_message="送出了 测试礼物 x2",
                    additional_config=json.dumps(info.additional_config, ensure_ascii=False),
                    is_command=False,
                    is_notify=True,
                )

            adapter._send_to_nachobot = persist_emitted_message
            manager = EventManager(
                config=SimpleNamespace(platform="bilibili.live"),
                logger=SimpleNamespace(info=Mock(), error=Mock()),
                adapter_ref=adapter,
            )
            key = (100, "12345", "测试礼物")
            manager.gift_buffer[key] = {
                "count": 2,
                "price": 10,
                "timestamp": occurrence_timestamp,
                "user_name": "测试用户",
            }
            manager.last_gift_time[key] = read_watermark - 3.0

            sleep_count = 0

            async def sleep_until_flushed(_seconds):
                nonlocal sleep_count
                sleep_count += 1
                if sleep_count > 1:
                    raise asyncio.CancelledError

            def schedule(coroutine):
                task = asyncio.get_running_loop().create_task(coroutine)
                scheduled_tasks.append(task)
                return task

            fake_asyncio = SimpleNamespace(
                sleep=sleep_until_flushed,
                create_task=schedule,
                CancelledError=asyncio.CancelledError,
            )
            fake_clock = SimpleNamespace(time=Mock(return_value=read_watermark + 50.0))
            with (
                patch.object(event_manager_module, "asyncio", fake_asyncio),
                patch.object(event_manager_module, "time", fake_clock),
            ):
                await manager.gift_flush_loop()
                await asyncio.gather(*scheduled_tasks)

            query_results = get_raw_msg_by_timestamp_with_chat(
                stream_id,
                read_watermark,
                read_watermark + 100.0,
                filter_command=True,
            )
            return read_watermark, occurrence_timestamp, query_results

        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(str(Path(directory) / "delayed-gift.db"))
            with database.bind_ctx([Messages]):
                database.connect()
                database.create_tables([Messages])
                try:
                    read_watermark, occurrence_timestamp, query_results = asyncio.run(exercise(database))
                finally:
                    database.drop_tables([Messages])
                    database.close()

        self.assertLess(occurrence_timestamp, read_watermark)
        self.assertEqual(len(query_results), 1)
        self.assertGreater(query_results[0].time, read_watermark)
        event = json.loads(query_results[0].additional_config)["system_event"]
        self.assertEqual(event["type"], "bilibili.gift")
        self.assertEqual(event["data"]["occurred_at"], occurrence_timestamp)


if __name__ == "__main__":
    unittest.main()
