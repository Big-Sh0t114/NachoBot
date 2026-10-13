import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.chat.utils import chat_message_builder as builder
from src.common.data_models.database_data_model import DatabaseMessages


def _row(message_id: str, timestamp: float, text: str, *, sender: bool = True) -> DatabaseMessages:
    kwargs = {}
    if sender:
        kwargs.update(user_id="sender-1", user_nickname="Sender", user_platform="qq")
    return DatabaseMessages(
        message_id=message_id,
        time=timestamp,
        chat_id="qq_private_sender-1",
        processed_plain_text=text,
        display_message=text,
        chat_info_stream_id="qq_private_sender-1",
        chat_info_platform="qq",
        **kwargs,
    )


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def where(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def __iter__(self):
        return iter(self.rows)


class ChatMessageBuilderAliasTests(unittest.TestCase):
    def setUp(self):
        self.person = patch.object(
            builder,
            "Person",
            side_effect=lambda **_kwargs: SimpleNamespace(person_name=""),
        )
        self.person.start()
        self.addCleanup(self.person.stop)
        self.resolve_name = patch.object(
            builder,
            "resolve_sender_name",
            side_effect=lambda **kwargs: kwargs["user_nickname"],
        )
        self.resolve_name.start()
        self.addCleanup(self.resolve_name.stop)
        self.user_refs = patch.object(builder, "replace_user_references", side_effect=lambda text, *_a, **_k: text)
        self.user_refs.start()
        self.addCleanup(self.user_refs.stop)

    def test_same_timestamp_rows_keep_their_alias_through_actions_and_filtered_rows(self):
        timestamp = 1_791_061_416.0
        first = _row("first", timestamp, "first recovered message")
        invalid = _row("invalid", timestamp, "row without a sender", sender=False)
        second = _row("second", timestamp, "second recovered message")
        action = SimpleNamespace(
            action_build_into_prompt=True,
            action_prompt_display="recorded action",
            time=timestamp,
            action_name="test_action",
            chat_info_platform="qq",
        )

        with patch.object(
            builder.ActionRecords,
            "select",
            side_effect=[_FakeQuery([action]), _FakeQuery([])],
        ):
            formatted, message_id_list = builder.build_readable_messages_with_id(
                messages=[first, invalid, second],
                timestamp_mode="normal_no_YMD",
                truncate=False,
                show_actions=True,
                show_pic=False,
            )

        aliases = {id(message): alias for alias, message in message_id_list}
        lines = formatted.splitlines()
        first_line = next(line for line in lines if "first recovered message" in line)
        second_line = next(line for line in lines if "second recovered message" in line)
        action_line = next(line for line in lines if "recorded action" in line)

        self.assertTrue(first_line.startswith(f"[{aliases[id(first)]}]"))
        self.assertTrue(second_line.startswith(f"[{aliases[id(second)]}]"))
        self.assertNotEqual(aliases[id(first)], aliases[id(second)])
        self.assertIn("recorded action", action_line)
        self.assertNotIn(f"[{aliases[id(first)]}]", action_line)
        self.assertNotIn(f"[{aliases[id(second)]}]", action_line)
        self.assertNotIn("row without a sender", formatted)

    def test_read_mark_and_truncation_preserve_each_source_alias(self):
        first = _row("before", 1_791_061_400.0, "old " * 40)
        second = _row("after", 1_791_061_500.0, "new message after the mark")

        formatted, message_id_list = builder.build_readable_messages_with_id(
            messages=[first, second],
            timestamp_mode="normal_no_YMD",
            read_mark=1_791_061_450.0,
            truncate=True,
            show_actions=False,
            show_pic=False,
        )

        aliases = {id(message): alias for alias, message in message_id_list}
        lines = formatted.splitlines()
        first_line = next(line for line in lines if "old " in line)
        second_line = next(line for line in lines if "new message after the mark" in line)
        self.assertTrue(first_line.startswith(f"[{aliases[id(first)]}]"))
        self.assertTrue(second_line.startswith(f"[{aliases[id(second)]}]"))
        self.assertIn("记不清了", first_line)
        self.assertIn("以上消息是你已经看过", formatted)


if __name__ == "__main__":
    unittest.main()
