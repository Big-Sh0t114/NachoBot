import tempfile
import unittest
from pathlib import Path

from peewee import SqliteDatabase

from src.chat.utils.chat_message_builder import get_raw_msg_by_timestamp_with_chat_inclusive
from src.common.database.database_model import Messages
from src.common.message_repository import find_messages


class MessageRepositoryCommandFilterTests(unittest.TestCase):
    @staticmethod
    def _create_message(message_id: str, timestamp: float, *, is_command: bool) -> None:
        Messages.create(
            message_id=message_id,
            time=timestamp,
            chat_id="stream-1",
            chat_info_stream_id="stream-1",
            chat_info_platform="qq",
            chat_info_user_platform="",
            chat_info_user_id="",
            chat_info_user_nickname="",
            chat_info_create_time=1.0,
            chat_info_last_active_time=timestamp,
            processed_plain_text=message_id,
            is_command=is_command,
        )

    def test_filter_command_keeps_ordinary_and_senderless_event_rows(self) -> None:
        """The Peewee predicate must exclude commands without dropping events."""
        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(str(Path(directory) / "messages.db"))
            with database.bind_ctx([Messages]):
                database.connect()
                database.create_tables([Messages])
                try:
                    self._create_message("ordinary", 1.0, is_command=False)
                    self._create_message("system-event", 2.0, is_command=False)
                    self._create_message("command", 3.0, is_command=True)

                    unrestricted = find_messages(
                        {"chat_id": "stream-1"},
                        sort=[("time", 1)],
                        filter_command=False,
                    )
                    filtered = find_messages(
                        {"chat_id": "stream-1"},
                        sort=[("time", 1)],
                        filter_command=True,
                    )
                    self.assertEqual(
                        [message.message_id for message in unrestricted],
                        ["ordinary", "system-event", "command"],
                    )
                    self.assertEqual(
                        [message.message_id for message in filtered],
                        ["ordinary", "system-event"],
                    )

                    latest = find_messages(
                        {"chat_id": "stream-1"},
                        limit=1,
                        limit_mode="latest",
                        filter_command=True,
                    )
                    earliest = find_messages(
                        {"chat_id": "stream-1"},
                        limit=1,
                        limit_mode="earliest",
                        filter_command=True,
                    )
                    self.assertEqual([message.message_id for message in latest], ["system-event"])
                    self.assertEqual([message.message_id for message in earliest], ["ordinary"])

                    inclusive = get_raw_msg_by_timestamp_with_chat_inclusive(
                        "stream-1",
                        1.0,
                        3.0,
                        filter_command=True,
                    )
                    self.assertEqual(
                        [message.message_id for message in inclusive],
                        ["ordinary", "system-event"],
                    )
                finally:
                    database.drop_tables([Messages])
                    database.close()


if __name__ == "__main__":
    unittest.main()
