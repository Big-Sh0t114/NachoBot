import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from peewee import SqliteDatabase

from ncnk_message import build_system_event, build_system_event_route
from src.chat.message_receive.bot import ChatBot
from src.chat.message_receive.chat_stream import ChatManager
from src.chat.message_receive.storage import MessageStorage
from src.common.database.database_model import ChatStreams, Messages
import src.chat.message_receive.bot as bot_module


class PrivateSystemEventPersistenceTests(unittest.TestCase):
    def test_private_event_uses_route_identity_without_sender_attribution(self) -> None:
        async def exercise(database: SqliteDatabase) -> tuple[object, list[object]]:
            old_instance = ChatManager._instance
            old_initialized = ChatManager._initialized
            ChatManager._instance = None
            ChatManager._initialized = False
            manager = None
            received: list[object] = []
            try:
                manager = ChatManager()
                event = build_system_event("qq.poke", actor={"user_id": "123", "name": "事件用户"})
                route = build_system_event_route("qq", user_id="123", nickname="私聊用户")

                async def process_message(message):
                    received.append(message)
                    await MessageStorage.store_message(message, message.chat_stream)

                bot = ChatBot()
                bot._ensure_started = AsyncMock()
                bot.handle_notice_message = AsyncMock(return_value=True)
                bot.heartflow_message_receiver.process_message = process_message

                with patch.object(bot_module, "get_chat_manager", return_value=manager), patch.object(
                    bot_module, "track_platform_event", new=AsyncMock()
                ):
                    await bot.message_process(
                        {
                            "message_info": {
                                "platform": "qq",
                                "message_id": "private-poke",
                                "time": 1.0,
                                "group_info": None,
                                "user_info": None,
                                "sender_info": None,
                                "format_info": {"content_format": ["text"], "accept_format": ["text"]},
                                "additional_config": {
                                    "system_event": event,
                                    "system_event_route": route,
                                },
                            },
                            "message_segment": {"type": "text", "data": "戳了戳NachoBot"},
                            "raw_message": "",
                        }
                    )
                    await asyncio.sleep(0)

                row = Messages.get(Messages.message_id == "private-poke")
                return row, received
            finally:
                ChatManager._instance = old_instance
                ChatManager._initialized = old_initialized
                if manager is not None:
                    manager.streams.clear()
                    manager.last_messages.clear()
                    manager.last_routing_user_infos.clear()

        async def run_with_database(database: SqliteDatabase):
            with database.bind_ctx([ChatStreams, Messages]):
                database.connect()
                database.create_tables([ChatStreams, Messages])
                try:
                    return await exercise(database)
                finally:
                    database.drop_tables([Messages, ChatStreams])
                    database.close()

        with tempfile.TemporaryDirectory() as directory:
            # ChatManager persists through ``asyncio.to_thread``.  A
            # thread-shared test connection lets the explicit close below
            # release the same SQLite handle on Windows.
            database = SqliteDatabase(
                str(Path(directory) / "system-events.db"),
                thread_safe=False,
                check_same_thread=False,
            )
            row, received = asyncio.run(run_with_database(database))

        self.assertEqual(len(received), 1)
        message = received[0]
        self.assertIsNone(message.message_info.user_info)
        self.assertIsNone(message.message_info.sender_info)
        self.assertIsNone(message.message_info.group_info)
        self.assertIsNone(row.user_platform)
        self.assertIsNone(row.user_id)
        self.assertIsNone(row.user_nickname)
        self.assertEqual(row.chat_info_user_platform, "qq")
        self.assertEqual(row.chat_info_user_id, "123")
        self.assertEqual(row.chat_info_user_nickname, "私聊用户")


if __name__ == "__main__":
    unittest.main()
