from __future__ import annotations

import unittest

from src.chat.sandbox.sandbox_handoff import (
    SandboxEditCandidate,
    parse_sandbox_confirmation,
)


class SandboxHandoffTests(unittest.TestCase):
    def test_foreign_json_with_reply_to_user_is_not_sandbox(self) -> None:
        content = (
            '{"ban_decision":true,'
            '"duration_minutes":10,'
            '"reply_to_user":"冷静一下"}'
        )

        result = parse_sandbox_confirmation(content, None)

        self.assertEqual(result.content, content)
        self.assertFalse(result.envelope_seen)
        self.assertIsNone(result.handoff)

    def test_fenced_ban_json_is_not_sandbox(self) -> None:
        content = """```json
{"ban_decision": false, "reply_to_user": "这次不禁言"}
```"""

        result = parse_sandbox_confirmation(content, None)

        self.assertEqual(result.content, content)
        self.assertFalse(result.envelope_seen)
        self.assertIsNone(result.handoff)

    def test_malformed_sandbox_envelope_fails_closed(self) -> None:
        content = '{"sandbox_edit_decision":true,"reply_to_user":"确认"}'

        result = parse_sandbox_confirmation(content, None)

        self.assertTrue(result.envelope_seen)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.handoff)

    def test_valid_sandbox_envelope_mints_handoff(self) -> None:
        candidate = SandboxEditCandidate.mint(
            stream_id="stream-1",
            platform="test",
            group_id="group-1",
            actor_id="actor-1",
            source_message_id="message-1",
            query="edit a file",
        )
        content = (
            '{"sandbox_edit_decision":true,'
            '"reply_to_user":"确认修改",'
            '"file_edit_query":"edit a file"}'
        )

        result = parse_sandbox_confirmation(content, candidate)

        self.assertEqual(result.content, "确认修改")
        self.assertTrue(result.envelope_seen)
        self.assertTrue(result.accepted)
        self.assertIsNotNone(result.handoff)
        self.assertEqual(result.handoff.file_edit_query, "edit a file")


if __name__ == "__main__":
    unittest.main()
