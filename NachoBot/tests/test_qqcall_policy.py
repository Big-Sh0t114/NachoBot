from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qqcall_client.qqcall_policy import group_status_decision, match_group_call_trigger  # noqa: E402


class TestQQCallPolicy(unittest.TestCase):
    def test_group_trigger_matcher_is_anchored_and_bounded(self):
        for text, expected in (
            ("#join_vc_qq", "command"),
            ("#join_vc_qq", "command"),
            ("加入群语音", "natural"),
            (" 请加入群语音！ ", "natural"),
            ("加入当前群通话。", "natural"),
            ("  加入当前群通话  ", "natural"),
        ):
            with self.subTest(text=text):
                self.assertEqual(match_group_call_trigger(text), expected)

        for text in (
            "请不要加入群语音",
            "引用：请加入群语音",
            "@bot 请加入群语音",
            "“请加入群语音”",
            "请加入群语音，然后继续聊天",
            "加入群语音\n",
            "x" * 129,
            None,
        ):
            with self.subTest(text=text):
                self.assertIsNone(match_group_call_trigger(text))

    def test_group_status_needs_fresh_exact_live_room_and_free_global_slot(self):
        meta = {"serviceInstance": "service-1", "hostGeneration": 3, "connectionGeneration": 8}
        now_ms = 1_800_000_000_000
        room = {
            "groupId": "246813579",
            "roomId": "room-private-opaque",
            "roomGeneration": 5,
            "live": True,
            "observedAtMs": now_ms - 100,
        }

        def status(**updates):
            value = {
                "version": 1,
                "accountId": "123456789",
                **meta,
                "groupId": "246813579",
                "occupied": False,
                "nativeJoinVerified": True,
                "room": dict(room),
            }
            value.update(updates)
            return value

        args = {
            "account_id": "123456789",
            "service_meta": meta,
            "group_id": "246813579",
            "local_call_scope": None,
            "now_ms": now_ms,
        }
        self.assertIsNone(group_status_decision(status(), **args))
        self.assertEqual(group_status_decision(status(occupied=True), **args), "call_occupied")
        self.assertEqual(
            group_status_decision(status(), **{**args, "local_call_scope": {"callId": "active"}}),
            "call_occupied",
        )
        self.assertEqual(
            group_status_decision(status(room=None), **args),
            "unsupported_unverified_group_join",
        )
        self.assertEqual(
            group_status_decision(status(room={**room, "observedAtMs": now_ms - 5_001}), **args),
            "unsupported_unverified_group_join",
        )
        self.assertEqual(
            group_status_decision(status(room={key: value for key, value in room.items() if key != "roomId"}), **args),
            "unsupported_unverified_group_join",
        )
        self.assertEqual(
            group_status_decision(status(room={**room, "groupId": "111222333"}), **args),
            "unsupported_unverified_group_join",
        )
        self.assertEqual(
            group_status_decision(status(), **{**args, "expected_room": {**room, "roomId": "replaced"}}),
            "group_room_changed",
        )
        self.assertIsNone(group_status_decision(
            status(room={**room, "observedAtMs": now_ms - 10}),
            **{**args, "expected_room": room},
        ))
        self.assertEqual(
            group_status_decision(status(), **{**args, "group_id": "0246813579"}),
            "group_id_invalid",
        )


if __name__ == "__main__":
    unittest.main()
