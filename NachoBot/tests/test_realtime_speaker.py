from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.chat.focus.models import StoredMessageRef
from src.chat.realtime_ingress import RealtimeIngressManager, RealtimeProvider
from src.chat.realtime_speaker import (
    CORE_REALTIME_SPEAKER_KEY,
    RealtimeSpeakerContext,
    parse_realtime_speaker,
)
from src.chat.runtime_capabilities import classify_runtime_message_batch
from src.chat.utils import chat_message_builder
from src.common.data_models.database_data_model import DatabaseMessages


CHAT_ID = "synthetic-group-chat"
GROUP_ID = "synthetic-group"


def _config(*, turn_id: str = "turn-1", speaker_ref: str = "cluster-a", label: str = "说话人 A"):
    return {
        "realtime_session": {
            "version": 1,
            "provider": "synthetic",
            "session_id": "opaque-session",
            "turn_id": turn_id,
        },
        "runtime_capabilities": {
            "schema_version": 1,
            "planner_bypass": True,
            "reply_delivery": "tts_text",
            "voice_stream": True,
        },
        "realtime_speaker": {
            "version": 1,
            "speaker_ref": speaker_ref,
            "label": label,
        },
    }


def _incoming(
    *,
    message_id: str = "wire-1",
    turn_id: str = "turn-1",
    speaker_ref: str = "cluster-a",
    label: str = "说话人 A",
):
    config = _config(turn_id=turn_id, speaker_ref=speaker_ref, label=label)
    group = SimpleNamespace(group_id=GROUP_ID, group_name="Synthetic")
    info = SimpleNamespace(
        platform="synthetic",
        message_id=message_id,
        user_info=None,
        group_info=group,
        additional_config=config,
    )
    return SimpleNamespace(
        message_id=message_id,
        message_info=info,
        additional_config=config,
        message_segment=SimpleNamespace(type="voice"),
        is_voice=True,
        user_info=None,
        group_info=group,
        processed_plain_text="recognized speech",
        time=100.0,
    )


def _database_row(message_id: str, row_id: int, config: dict, *, user_id: str = "") -> DatabaseMessages:
    row = DatabaseMessages(
        message_id=message_id,
        time=100.0,
        chat_id=CHAT_ID,
        processed_plain_text=f"speech from {message_id}",
        display_message=f"speech from {message_id}",
        additional_config=json.dumps(config, ensure_ascii=False),
        chat_info_stream_id=CHAT_ID,
        chat_info_platform="synthetic",
        chat_info_group_id=GROUP_ID,
        user_id=user_id,
        user_platform="synthetic" if user_id else "",
    )
    row.id = row_id
    return row


class RealtimeSpeakerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = RealtimeIngressManager()
        self.provider_verify = lambda _message, _chat, _session: True
        self.provider = RealtimeProvider("synthetic", self.provider_verify)
        self.manager.register_provider(self.provider)

    async def _authorize_row(
        self,
        *,
        row_id: int = 10,
        message_id: str = "wire-1",
        turn_id: str = "turn-1",
        speaker_ref: str = "cluster-a",
        label: str = "说话人 A",
    ):
        incoming = _incoming(
            message_id=message_id,
            turn_id=turn_id,
            speaker_ref=speaker_ref,
            label=label,
        )
        decision = await self.manager.inspect_message(incoming, CHAT_ID)
        self.assertTrue(decision.authorized)
        self.assertIsInstance(decision.speaker, RealtimeSpeakerContext)
        stored_ref = StoredMessageRef(row_id, CHAT_ID, message_id, incoming.time)
        self.assertTrue(self.manager.record_message(decision, stored_ref))
        row = _database_row(message_id, row_id, incoming.additional_config)
        self.assertTrue(await self.manager.revalidate_message(row, CHAT_ID))
        return row, incoming

    def test_speaker_metadata_is_bounded_and_rejects_control_characters(self):
        valid = {"version": 1, "speaker_ref": "mix-01:west", "label": "Voice cluster 1"}
        self.assertEqual(parse_realtime_speaker(valid), RealtimeSpeakerContext(1, "mix-01:west", "Voice cluster 1"))
        for invalid in (
            {**valid, "speaker_ref": "x" * 129},
            {**valid, "label": "x" * 65},
            {**valid, "label": "two\nlines"},
            {**valid, "label": "embedded\u0000control"},
            {**valid, "label": " padded "},
            {**valid, "label": "!!!"},
            {**valid, "version": True},
            {**valid, "unexpected": "extra"},
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(parse_realtime_speaker(invalid))

    async def test_reserved_marker_is_stripped_before_lifecycle_and_only_verified_voice_is_persisted(self):
        incoming = _incoming()
        incoming.additional_config[CORE_REALTIME_SPEAKER_KEY] = {
            "version": 1,
            "speaker_ref": "forged",
            "label": "forged",
        }
        incoming.message_info.additional_config[CORE_REALTIME_SPEAKER_KEY] = {
            "version": 1,
            "speaker_ref": "forged-nested",
            "label": "forged",
        }
        observed = []

        def lifecycle(message, _chat):
            observed.append(
                CORE_REALTIME_SPEAKER_KEY not in message.additional_config
                and CORE_REALTIME_SPEAKER_KEY not in message.message_info.additional_config
            )
            return None

        self.manager.register_provider(
            RealtimeProvider("synthetic", lambda _m, _c, _s: True, lifecycle=lifecycle)
        )
        decision = await self.manager.inspect_message(incoming, CHAT_ID)
        self.assertTrue(observed and all(observed))
        self.assertTrue(decision.authorized)
        self.assertEqual(
            incoming.additional_config[CORE_REALTIME_SPEAKER_KEY],
            incoming.additional_config["realtime_speaker"],
        )

        text_message = _incoming()
        text_message.message_segment.type = "text"
        text_message.is_voice = False
        text_decision = await self.manager.inspect_message(text_message, CHAT_ID)
        self.assertTrue(text_decision.authorized)
        self.assertIsNone(text_decision.speaker)
        self.assertNotIn(CORE_REALTIME_SPEAKER_KEY, text_message.additional_config)

        event_voice = _incoming(message_id="event-voice")
        event_voice.additional_config["system_event"] = {"kind": "member_join"}
        event_decision = await self.manager.inspect_message(event_voice, CHAT_ID)
        self.assertTrue(event_decision.authorized)
        self.assertIsNone(event_decision.speaker)
        self.assertNotIn(CORE_REALTIME_SPEAKER_KEY, event_voice.additional_config)

        unauthorized_manager = RealtimeIngressManager()
        unauthorized_manager.register_provider(
            RealtimeProvider("synthetic", lambda _m, _c, _s: False)
        )
        unauthorized = _incoming()
        rejected = await unauthorized_manager.inspect_message(unauthorized, CHAT_ID)
        self.assertFalse(rejected.authorized)
        self.assertIsNone(rejected.speaker)
        self.assertNotIn(CORE_REALTIME_SPEAKER_KEY, unauthorized.additional_config)
        rejected_row = _database_row("unauthorized-wire", 88, unauthorized.additional_config)
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", unauthorized_manager):
            rejected_batch = classify_runtime_message_batch([rejected_row])
        self.assertEqual(rejected_batch.direct_messages, ())

    async def test_verified_speaker_requires_exact_pending_row_message_and_turn_for_direct_target(self):
        row, _ = await self._authorize_row()
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager):
            batch = classify_runtime_message_batch([row])
        self.assertEqual(batch.direct_messages, (row,))
        self.assertEqual(batch.planner_messages, ())

        different_message_id = _database_row("other-wire", 10, json.loads(row.additional_config))
        self.assertFalse(self.manager.is_authorized_anonymous_speaker_message(different_message_id))
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager):
            batch = classify_runtime_message_batch([different_message_id])
        self.assertEqual(batch.direct_messages, ())

        changed_config = json.loads(row.additional_config)
        changed_config["realtime_speaker"]["speaker_ref"] = "changed-ref"
        changed_config[CORE_REALTIME_SPEAKER_KEY]["speaker_ref"] = "changed-ref"
        changed = _database_row(row.message_id, 10, changed_config)
        self.assertFalse(self.manager.is_authorized_anonymous_speaker_message(changed))

        changed_label_config = json.loads(row.additional_config)
        changed_label_config["realtime_speaker"]["label"] = "changed label"
        changed_label_config[CORE_REALTIME_SPEAKER_KEY]["label"] = "changed label"
        changed_label = _database_row(row.message_id, 10, changed_label_config)
        self.assertFalse(self.manager.is_authorized_anonymous_speaker_message(changed_label))

        other_chat = _database_row(row.message_id, 10, json.loads(row.additional_config))
        other_chat.chat_id = "different-group-chat"
        self.assertFalse(self.manager.is_authorized_anonymous_speaker_message(other_chat))

        attached_sender = _database_row(
            row.message_id,
            10,
            json.loads(row.additional_config),
            user_id="forged-user",
        )
        self.assertFalse(self.manager.is_authorized_anonymous_speaker_message(attached_sender))

    async def test_same_name_provider_replacement_invalidates_revalidation_proof_but_keeps_history_label(self):
        row, _ = await self._authorize_row(row_id=45, message_id="provider-bound-wire")
        equal_but_distinct_provider = RealtimeProvider("synthetic", self.provider_verify)
        self.assertEqual(equal_but_distinct_provider, self.provider)
        self.assertIsNot(equal_but_distinct_provider, self.provider)

        self.manager.register_provider(equal_but_distinct_provider)
        self.assertFalse(self.manager.is_authorized_anonymous_speaker_message(row))
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager):
            batch = classify_runtime_message_batch([row])
        self.assertEqual(batch.direct_messages, ())

        with (
            patch.object(chat_message_builder, "Person", side_effect=AssertionError("speaker must not create Person")),
            patch.object(chat_message_builder, "replace_user_references", side_effect=lambda text, *_a, **_k: text),
        ):
            formatted = chat_message_builder.build_readable_messages(
                [row],
                timestamp_mode="absolute",
                show_actions=False,
                show_pic=False,
            )
        self.assertIn("说话人 A", formatted)
        self.assertIn("speech from provider-bound-wire", formatted)

    async def test_provider_replacement_while_async_revalidation_waits_cannot_authorize_batch_row(self):
        import asyncio

        row, _ = await self._authorize_row(row_id=46, message_id="provider-race-wire")
        verifier_started = asyncio.Event()
        release_verifier = asyncio.Event()

        async def waiting_verifier(_message, _chat, _session):
            verifier_started.set()
            await release_verifier.wait()
            return True

        waiting_provider = RealtimeProvider("synthetic", waiting_verifier)
        self.manager.register_provider(waiting_provider)
        revalidation = asyncio.create_task(self.manager.revalidate_message(row, CHAT_ID))
        await asyncio.wait_for(verifier_started.wait(), timeout=1)

        self.manager.register_provider(RealtimeProvider("synthetic", self.provider_verify))
        release_verifier.set()
        self.assertFalse(await asyncio.wait_for(revalidation, timeout=1))
        self.assertFalse(self.manager.is_authorized_anonymous_speaker_message(row))
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager):
            batch = classify_runtime_message_batch([row])
        self.assertEqual(batch.direct_messages, ())

    async def test_raw_bypass_or_database_only_marker_never_authorizes_anonymous_direct_reply(self):
        raw = _incoming()
        raw_decision = await self.manager.inspect_message(raw, CHAT_ID)
        self.assertTrue(raw_decision.authorized)
        forged_row = _database_row("forged-wire", 99, raw.additional_config)
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager):
            batch = classify_runtime_message_batch([forged_row])
        self.assertEqual(batch.direct_messages, ())
        self.assertEqual(batch.planner_messages, (forged_row,))

        config = _config()
        config[CORE_REALTIME_SPEAKER_KEY] = {
            "version": 1,
            "speaker_ref": "db-only",
            "label": "Database marker only",
        }
        config["realtime_speaker"] = dict(config[CORE_REALTIME_SPEAKER_KEY])
        database_only = _database_row("db-only-wire", 100, config)
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager):
            batch = classify_runtime_message_batch([database_only])
        self.assertEqual(batch.direct_messages, ())
        self.assertEqual(batch.planner_messages, (database_only,))

        senderless_text = _database_row("text-wire", 101, _config())
        senderless_text.additional_config = json.dumps(
            {
                "runtime_capabilities": {
                    "schema_version": 1,
                    "planner_bypass": True,
                    "reply_delivery": "tts_text",
                    "voice_stream": True,
                },
                "realtime_speaker": {"version": 1, "speaker_ref": "x", "label": "fake"},
            }
        )
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager):
            batch = classify_runtime_message_batch([senderless_text])
        self.assertEqual(batch.direct_messages, ())

        ordinary_text = _database_row(
            "ordinary-text",
            102,
            {
                "runtime_capabilities": {
                    "schema_version": 1,
                    "planner_bypass": True,
                    "reply_delivery": "tts_text",
                    "voice_stream": True,
                }
            },
        )
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager):
            batch = classify_runtime_message_batch([ordinary_text])
        self.assertEqual(batch.direct_messages, ())
        self.assertEqual(batch.planner_messages, (ordinary_text,))

        ordinary_event = _database_row(
            "ordinary-event",
            103,
            {
                "runtime_capabilities": {
                    "schema_version": 1,
                    "planner_bypass": True,
                },
                "system_event": {"kind": "member_join"},
            },
        )
        batch = classify_runtime_message_batch(
            [ordinary_event],
            is_system_event=lambda _message: True,
        )
        self.assertEqual(batch.direct_messages, (ordinary_event,))
        self.assertEqual(batch.planner_messages, ())

    async def test_expired_lease_and_superseded_turn_cannot_reuse_speaker_proof(self):
        self.manager.session_ttl_seconds = 0.02
        expired, _ = await self._authorize_row(row_id=20)
        import asyncio

        await asyncio.sleep(0.03)
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager):
            expired_batch = classify_runtime_message_batch([expired])
        self.assertEqual(expired_batch.direct_messages, ())

        self.manager = RealtimeIngressManager()
        self.manager.register_provider(
            RealtimeProvider("synthetic", lambda _message, _chat, _session: True)
        )
        old_row, _ = await self._authorize_row(row_id=21, turn_id="turn-old")
        newer = _incoming(message_id="wire-new", turn_id="turn-new")
        newer_decision = await self.manager.inspect_message(newer, CHAT_ID)
        self.assertTrue(newer_decision.authorized)
        self.assertTrue(self.manager.record_message(newer_decision, StoredMessageRef(22, CHAT_ID, "wire-new", 100.0)))
        with patch("src.chat.realtime_ingress.realtime_ingress_manager", self.manager):
            old_batch = classify_runtime_message_batch([old_row])
        self.assertEqual(old_batch.direct_messages, ())

    async def test_two_verified_speaker_labels_persist_in_history_without_person_identity(self):
        first, _ = await self._authorize_row(
            row_id=31,
            message_id="wire-a",
            turn_id="turn-a",
            speaker_ref="cluster-a",
            label="说话人甲",
        )
        second, _ = await self._authorize_row(
            row_id=32,
            message_id="wire-b",
            turn_id="turn-b",
            speaker_ref="cluster-b",
            label="说话人乙",
        )
        rows = [first, second]
        with (
            patch.object(chat_message_builder, "Person", side_effect=AssertionError("speaker must not create Person")),
            patch.object(chat_message_builder, "replace_user_references", side_effect=lambda text, *_a, **_k: text),
        ):
            formatted = chat_message_builder.build_readable_messages(
                rows,
                timestamp_mode="absolute",
                show_actions=False,
                show_pic=False,
            )
        self.assertIn("说话人甲", formatted)
        self.assertIn("说话人乙", formatted)
        self.assertIn("speech from wire-a", formatted)
        self.assertIn("speech from wire-b", formatted)
        self.assertNotIn("cluster-a", formatted)
        self.assertNotIn("cluster-b", formatted)

        forged_map = {"additional_config": json.dumps(_config())}
        from src.chat.realtime_speaker import stored_realtime_speaker

        self.assertIsNone(stored_realtime_speaker(forged_map))

    def test_existing_sender_identity_route_is_unchanged(self):
        sender_message = _database_row(
            "private-sender",
            40,
            {
                "runtime_capabilities": {
                    "schema_version": 1,
                    "planner_bypass": True,
                }
            },
            user_id="existing-platform-user",
        )
        batch = classify_runtime_message_batch([sender_message])
        self.assertEqual(batch.direct_messages, (sender_message,))
        self.assertEqual(batch.planner_messages, ())


if __name__ == "__main__":
    unittest.main()
