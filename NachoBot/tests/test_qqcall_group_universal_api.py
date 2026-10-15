from __future__ import annotations

import asyncio
import base64
import logging
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QQCALL_ROOT = PROJECT_ROOT.parent / "NachoBot-QQCall"
for _path in (PROJECT_ROOT, QQCALL_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from qqcall_client import qqcall_group_universal_api as group_api  # noqa: E402
from qqcall_client.qqcall_group_universal_api import (  # noqa: E402
    QQCallGroupBindingError,
    QQCallGroupUniversalBinding,
)
from universalvc_client import MAX_PCM_FRAME_BYTES, PROTOCOL_VERSION  # noqa: E402


SYNTHETIC_VOICE_BASE64 = base64.b64encode(b"synthetic voice fixture").decode("ascii")
SYNTHETIC_ASR_RESULT_ID = "asr-result-secret-123"
SYNTHETIC_ASR_SCOPE = "asr-scope-secret-456"

SCOPE = {
    "kind": "qqcall_group",
    "serviceInstance": "test-service",
    "hostGeneration": 2,
    "connectionGeneration": 3,
    "groupId": "123456789",
    "roomId": "room-reference-1",
    "roomGeneration": 4,
}


class FakeSession:
    def __init__(self, *, session_id: str = "session-1", session_generation: str = "1") -> None:
        self.service_instance_id = "uvc-service-1"
        self.session_id = session_id
        self.session_generation = session_generation
        self.events_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.sent: list[tuple[bytes, str]] = []
        self.close_count = 0
        self.send_gate: asyncio.Event | None = None
        self.send_entered: asyncio.Event | None = None

    async def send_pcm(self, pcm: bytes, *, source_id: str = "main") -> None:
        if self.send_entered is not None:
            self.send_entered.set()
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.sent.append((pcm, source_id))

    async def events(self):
        while True:
            event = await self.events_queue.get()
            if event is None:
                return
            yield event

    async def push(self, event: dict[str, Any]) -> None:
        await self.events_queue.put(event)

    async def close(self) -> None:
        self.close_count += 1
        await self.events_queue.put(None)


class FakeClient:
    def __init__(self, session: FakeSession | None = None) -> None:
        self.session = session or FakeSession()
        self.create_count = 0
        self.options: list[dict[str, Any]] = []
        self.closed = False
        self.create_entered: asyncio.Event | None = None
        self.create_release: asyncio.Event | None = None
        self.ignore_create_cancellation = False

    async def create_session(self, options: dict[str, Any]) -> FakeSession:
        self.create_count += 1
        self.options.append(options)
        if self.create_entered is not None:
            self.create_entered.set()
        if self.create_release is not None:
            try:
                await self.create_release.wait()
            except asyncio.CancelledError:
                if not self.ignore_create_cancellation:
                    raise
                await self.create_release.wait()
        return self.session

    async def close(self) -> None:
        self.closed = True


def make_event(
    session: FakeSession,
    *,
    turn_id: str = "turn-1",
    speaker_id: str = "cluster-1",
    text: str = "hello",
    **overrides: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "event",
        "protocol_version": PROTOCOL_VERSION,
        "service_instance_id": session.service_instance_id,
        "session_id": session.session_id,
        "session_generation": session.session_generation,
        "source_id": "main",
        "event": "speech_result",
        "turn_id": turn_id,
        "speaker_id": speaker_id,
        "speaker_name": "must-not-escape",
        "text": text,
        "precomputed_asr_text": text,
        "precomputed_asr_result_id": SYNTHETIC_ASR_RESULT_ID,
        "asr_scope": SYNTHETIC_ASR_SCOPE,
        "voice_base64": SYNTHETIC_VOICE_BASE64,
    }
    event.update(overrides)
    return event


async def wait_until(predicate, *, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


class QQCallGroupUniversalBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_wav_without_streaming_receipt_keeps_binding_active(self) -> None:
        binding, client, received = self.make_binding()
        await binding.start(dict(SCOPE))
        event = make_event(client.session, text=None, precomputed_asr_result_id=None)
        await client.session.push(event)
        for _ in range(100):
            if received or binding.state == "failed":
                break
            await asyncio.sleep(0.005)
        self.assertEqual(len(received), 1)
        result = received[0]["speech_result"]
        self.assertIsNone(result["text"])
        self.assertIsNone(result["precomputed_asr_result_id"])
        self.assertEqual(result["voice_base64"], SYNTHETIC_VOICE_BASE64)
        await binding.send_pcm(dict(SCOPE), b"\0\0")
        self.assertEqual(binding.state, "active")
        self.assertEqual(client.session.sent, [(b"\0\0", "main")])
        await binding.close()

    async def test_pcm_failure_logs_fixed_reason_without_exception_details(self) -> None:
        cases = [
            (TimeoutError("private endpoint"), "pcm_send_timeout"),
            (group_api.UniversalVCDisconnected("private session"), "pcm_send_disconnected"),
            (group_api.UniversalVCProtocolError("private identity"), "pcm_send_protocol_invalid"),
            (group_api.UniversalVCServiceError("invalid_request", "private body"), "pcm_send_invalid_request"),
            (group_api.UniversalVCServiceError("unavailable", "private body"), "pcm_send_service_unavailable"),
            (group_api.UniversalVCServiceError("operation_failed", "private body"), "pcm_send_operation_failed"),
            (group_api.UniversalVCServiceError("private-token", "private body"), "pcm_send_failed"),
        ]
        for error, reason in cases:
            with self.subTest(reason=reason):
                binding, client, received = self.make_binding()
                await binding.start(dict(SCOPE))
                client.session.send_pcm = AsyncMock(side_effect=error)
                with patch.object(group_api._LOGGER, "warning") as warning:
                    with self.assertRaisesRegex(QQCallGroupBindingError, "pcm_send_failed"):
                        await binding.send_pcm(dict(SCOPE), b"\0\0")
                    self.assertEqual(warning.call_args.args, ("QQCall group UniversalVC binding failed code=%s", reason))
                    self.assertNotIn("private", repr(warning.call_args))
                self.assertEqual(binding.failure_code, reason)
                self.assertEqual(client.session.close_count, 1)
                self.assertEqual(received, [])
                self.assertFalse(client.closed)

    async def test_service_error_event_retains_only_allowlisted_reason(self) -> None:
        for code, expected in [("capture_discontinuity", "service_capture_discontinuity"),
                               ("event_too_large", "service_event_too_large"),
                               ("private-token", "service_event_error")]:
            with self.subTest(code=code):
                binding, client, received = self.make_binding()
                await binding.start(dict(SCOPE))
                with patch.object(group_api._LOGGER, "warning") as warning:
                    await client.session.push(make_event(client.session, event="error", code=code, message="private body"))
                    for _ in range(100):
                        if binding.state == "failed" and client.session.close_count:
                            break
                        await asyncio.sleep(0.005)
                    self.assertEqual(binding.failure_code, expected)
                    self.assertEqual(warning.call_args.args, ("QQCall group UniversalVC binding failed code=%s", expected))
                    self.assertNotIn("private", repr(warning.call_args))
                self.assertEqual(received, [])
                self.assertEqual(client.session.close_count, 1)
                self.assertFalse(client.closed)

    def make_binding(self, *, client=None, validator=None, callback=None):
        client = client or FakeClient()
        accepted = validator or (lambda _scope: asyncio.sleep(0, result=True))
        seen: list[dict[str, Any]] = []

        async def on_result(envelope: dict[str, Any]) -> None:
            seen.append(envelope)
            if callback is not None:
                await callback(envelope)

        return QQCallGroupUniversalBinding(client, accepted, on_result), client, seen

    async def test_disabled_lease_does_not_create_a_session_or_close_shared_client(self) -> None:
        client = FakeClient()
        binding, _, _ = self.make_binding(client=client, validator=lambda _scope: asyncio.sleep(0, result=False))
        with self.assertRaisesRegex(QQCallGroupBindingError, "lease_not_current"):
            await binding.start(dict(SCOPE))
        self.assertEqual(client.create_count, 0)
        self.assertFalse(client.closed)

    async def test_truthy_non_boolean_lease_does_not_authorize_session_creation(self) -> None:
        binding, client, _ = self.make_binding(
            validator=lambda _scope: asyncio.sleep(0, result={"valid": True})
        )
        with self.assertRaisesRegex(QQCallGroupBindingError, "lease_not_current"):
            await binding.start(dict(SCOPE))
        self.assertEqual(client.create_count, 0)

    async def test_lease_revoked_during_create_closes_only_new_session(self) -> None:
        checks = 0

        async def validator(_scope: dict[str, Any]) -> bool:
            nonlocal checks
            checks += 1
            return checks == 1

        client = FakeClient()
        binding, _, _ = self.make_binding(client=client, validator=validator)
        with self.assertRaisesRegex(QQCallGroupBindingError, "lease_not_current"):
            await binding.start(dict(SCOPE))
        self.assertEqual(checks, 2)
        self.assertEqual(client.create_count, 1)
        self.assertEqual(client.session.close_count, 1)
        self.assertFalse(client.closed)

    async def test_repeated_active_start_revalidates_lease(self) -> None:
        lease_current = True

        async def validator(_scope: dict[str, Any]) -> bool:
            return lease_current

        binding, client, _ = self.make_binding(validator=validator)
        with patch.object(group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
            await binding.start(dict(SCOPE))
            lease_current = False
            with self.assertRaisesRegex(QQCallGroupBindingError, "lease_not_current"):
                await binding.start(dict(SCOPE))
        self.assertEqual(client.create_count, 1)
        self.assertEqual(client.session.close_count, 1)
        self.assertEqual(binding.state, "failed")
        self.assertEqual(binding.failure_code, "lease_not_current")
        self.assertFalse(client.closed)

    async def test_idle_lease_monitor_revokes_and_closes_owned_session(self) -> None:
        lease_current = True

        async def validator(_scope: dict[str, Any]) -> bool:
            return lease_current

        binding, client, _ = self.make_binding(validator=validator)
        with patch.object(group_api, "_LEASE_MONITOR_INTERVAL", 0.01):
            await binding.start(dict(SCOPE))
            lease_current = False
            await wait_until(lambda: client.session.close_count == 1, timeout=2.0)
        self.assertEqual(binding.state, "failed")
        self.assertEqual(binding.failure_code, "lease_not_current")
        self.assertFalse(client.closed)

    async def test_lease_monitor_bounds_and_cancels_a_hung_validator(self) -> None:
        checks = 0
        validation_cancelled = asyncio.Event()
        never = asyncio.Event()

        async def validator(_scope: dict[str, Any]) -> bool:
            nonlocal checks
            checks += 1
            if checks <= 2:
                return True
            try:
                await never.wait()
            except asyncio.CancelledError:
                validation_cancelled.set()
                raise

        binding, client, _ = self.make_binding(validator=validator)
        with (
            patch.object(group_api, "_LEASE_MONITOR_INTERVAL", 0.01),
            patch.object(group_api, "_LEASE_TIMEOUT", 0.01),
        ):
            await binding.start(dict(SCOPE))
            await asyncio.wait_for(validation_cancelled.wait(), timeout=1.0)
            await wait_until(lambda: client.session.close_count == 1, timeout=1.0)
        self.assertEqual(binding.state, "failed")
        self.assertEqual(binding.failure_code, "lease_not_current")
        await wait_until(lambda: not binding._background_tasks, timeout=1.0)

    async def test_cancelled_start_cancels_and_drains_lease_validator(self) -> None:
        validator_started = asyncio.Event()
        validator_cancelled = asyncio.Event()
        never = asyncio.Event()

        async def validator(_scope: dict[str, Any]) -> bool:
            validator_started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                validator_cancelled.set()
                raise

        binding, client, _ = self.make_binding(validator=validator)
        start_task = asyncio.create_task(binding.start(dict(SCOPE)))
        await asyncio.wait_for(validator_started.wait(), timeout=1.0)
        start_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await start_task
        await asyncio.wait_for(validator_cancelled.wait(), timeout=1.0)
        await wait_until(lambda: not binding._background_tasks, timeout=1.0)
        self.assertEqual(client.create_count, 0)
        self.assertFalse(client.closed)

    async def test_close_cancels_and_drains_inflight_lease_validator(self) -> None:
        checks = 0
        validator_started = asyncio.Event()
        validator_cancelled = asyncio.Event()
        never = asyncio.Event()

        async def validator(_scope: dict[str, Any]) -> bool:
            nonlocal checks
            checks += 1
            if checks <= 2:
                return True
            validator_started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                validator_cancelled.set()
                raise

        binding, client, _ = self.make_binding(validator=validator)
        with patch.object(group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
            await binding.start(dict(SCOPE))
            send_task = asyncio.create_task(binding.send_pcm(dict(SCOPE), b"\x01\x02"))
            await asyncio.wait_for(validator_started.wait(), timeout=1.0)
            await binding.close()
            with self.assertRaises(asyncio.CancelledError):
                await send_task
        await asyncio.wait_for(validator_cancelled.wait(), timeout=1.0)
        await wait_until(lambda: not binding._background_tasks, timeout=1.0)
        self.assertEqual(client.session.close_count, 1)
        self.assertFalse(client.closed)

    async def test_send_detects_session_identity_change_and_closes_owned_session(self) -> None:
        binding, client, _ = self.make_binding()
        with patch.object(group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
            await binding.start(dict(SCOPE))
            client.session.session_generation = "generation-changed"
            with self.assertRaisesRegex(QQCallGroupBindingError, "session_identity_changed"):
                await binding.send_pcm(dict(SCOPE), b"\x01\x02")
        self.assertEqual(client.session.sent, [])
        self.assertEqual(client.session.close_count, 1)
        self.assertEqual(binding.state, "failed")
        self.assertEqual(binding.failure_code, "session_identity_changed")
        self.assertFalse(client.closed)

    async def test_send_rechecks_identity_after_sdk_await(self) -> None:
        session = FakeSession()
        session.send_gate = asyncio.Event()
        session.send_entered = asyncio.Event()
        binding, client, _ = self.make_binding(client=FakeClient(session))
        with patch.object(group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
            await binding.start(dict(SCOPE))
            send_task = asyncio.create_task(binding.send_pcm(dict(SCOPE), b"\x01\x02"))
            await asyncio.wait_for(session.send_entered.wait(), timeout=1.0)
            session.session_generation = "generation-changed-during-send"
            session.send_gate.set()
            with self.assertRaisesRegex(QQCallGroupBindingError, "session_identity_changed"):
                await send_task
        self.assertEqual(session.close_count, 1)
        self.assertEqual(binding.state, "failed")
        self.assertEqual(binding.failure_code, "session_identity_changed")
        self.assertEqual(binding._send_tasks, set())
        self.assertFalse(client.closed)

    async def test_wrong_event_source_fails_closed_without_callback(self) -> None:
        binding, client, seen = self.make_binding()
        with patch.object(group_api, "_LEASE_MONITOR_INTERVAL", 60.0):
            await binding.start(dict(SCOPE))
            await client.session.push(make_event(client.session, source_id="secondary"))
            await wait_until(lambda: client.session.close_count == 1)
        self.assertEqual(seen, [])
        self.assertEqual(binding.state, "failed")

    async def test_scope_is_immutable_and_pcm_is_bounded_bytes_only(self) -> None:
        binding, client, _ = self.make_binding()
        original = dict(SCOPE)
        await binding.start(original)
        original["roomId"] = "changed"
        with self.assertRaisesRegex(QQCallGroupBindingError, "scope_mismatch"):
            await binding.send_pcm(original, b"\x00\x00")

        for invalid in (b"", b"\x00", b"\x00" * (MAX_PCM_FRAME_BYTES + 2), bytearray(b"\x00\x00")):
            with self.assertRaisesRegex(QQCallGroupBindingError, "pcm_frame_invalid"):
                await binding.send_pcm(dict(SCOPE), invalid)  # type: ignore[arg-type]
        await binding.send_pcm(dict(SCOPE), b"\x01\x02")
        self.assertEqual(client.session.sent, [(b"\x01\x02", "main")])
        await binding.close()

    async def test_created_session_identity_must_be_valid_and_bound(self) -> None:
        invalid = FakeSession(session_generation="")
        client = FakeClient(invalid)
        binding, _, _ = self.make_binding(client=client)
        with self.assertRaisesRegex(QQCallGroupBindingError, "session_identity_invalid"):
            await binding.start(dict(SCOPE))
        self.assertEqual(invalid.close_count, 1)

    async def test_wrong_event_session_identity_fails_closed_without_callback_or_log(self) -> None:
        for event_name in ("speech_result", "speech_start"):
            with self.subTest(event=event_name):
                binding, client, seen = self.make_binding()
                await binding.start(dict(SCOPE))
                invalid = make_event(client.session, session_id="other-session", event=event_name)
                with patch.object(group_api._LOGGER, "info") as logger_info:
                    await client.session.push(invalid)
                    await wait_until(lambda: client.session.close_count == 1)
                self.assertEqual(seen, [])
                logger_info.assert_not_called()

    async def test_speech_results_are_anonymous_deduplicated_and_logged_safely(self) -> None:
        binding, client, seen = self.make_binding()
        await binding.start(dict(SCOPE))
        session = client.session
        with self.assertLogs(group_api.__name__, level="INFO") as captured:
            await session.push(make_event(session, turn_id="turn-a", speaker_id="cluster-a", text="first"))
            await session.push(make_event(session, turn_id="turn-a", speaker_id="cluster-a", text="redelivery"))
            await session.push(make_event(session, turn_id="turn-b", speaker_id="cluster-b", text="second"))
            await wait_until(lambda: len(seen) == 2)
            await wait_until(lambda: len(captured.records) == 2)

        self.assertEqual([item["speech_result"]["text"] for item in seen], ["first", "second"])
        for item in seen:
            self.assertEqual(
                item["speech_result"],
                {
                    "turn_id": item["speech_result"]["turn_id"],
                    "text": item["speech_result"]["text"],
                    "voice_base64": SYNTHETIC_VOICE_BASE64,
                    "precomputed_asr_result_id": SYNTHETIC_ASR_RESULT_ID,
                    "asr_scope": SYNTHETIC_ASR_SCOPE,
                },
            )
            self.assertEqual(
                set(item["speech_result"]),
                {"turn_id", "text", "voice_base64", "precomputed_asr_result_id", "asr_scope"},
            )
        first, second = (item["speaker_annotation"] for item in seen)
        self.assertNotEqual(first["speaker_ref"], second["speaker_ref"])
        for envelope in seen:
            self.assertEqual(envelope["scope"], SCOPE)
            self.assertEqual(envelope["source_id"], "main")
            self.assertNotIn("speaker_id", envelope)
            self.assertNotIn("speaker_name", envelope)
            self.assertNotIn("voice_base64", envelope)
            self.assertNotIn("precomputed_asr_result_id", envelope)
        self.assertEqual(len(captured.records), 2)
        combined_logs = "\n".join(record.getMessage() for record in captured.records)
        for secret in (
            "cluster-a", "cluster-b", "must-not-escape", "first", "second", "123456789",
            SYNTHETIC_VOICE_BASE64, SYNTHETIC_ASR_RESULT_ID, SYNTHETIC_ASR_SCOPE,
        ):
            self.assertNotIn(secret, combined_logs)
        self.assertIn(first["speaker_ref"], combined_logs)
        self.assertIn(second["speaker_ref"], combined_logs)
        self.assertIn("turn_ref=qqt-", combined_logs)
        await binding.close()

    async def test_verify_result_exposes_only_live_accepted_turn_reference(self) -> None:
        callback_entered = asyncio.Event()
        callback_release = asyncio.Event()
        lease_current = True

        async def validator(_scope: dict[str, Any]) -> bool:
            return lease_current

        async def callback(_envelope: dict[str, Any]) -> None:
            callback_entered.set()
            await callback_release.wait()

        binding, client, seen = self.make_binding(validator=validator, callback=callback)
        with patch.object(group_api, "_LEASE_MONITOR_INTERVAL", 0.01):
            await binding.start(dict(SCOPE))
            await client.session.push(make_event(client.session, turn_id="proof-turn", speaker_id="anonymous-cluster"))
            await asyncio.wait_for(callback_entered.wait(), timeout=1.0)
            await wait_until(lambda: len(seen) == 1)
            envelope = seen[0]
            claim = {
                "turn_id": "proof-turn",
                "uvc_session": envelope["session_identity"],
                "speaker_ref": envelope["speaker_annotation"]["speaker_ref"],
            }

            self.assertTrue(await binding.verify_result(dict(SCOPE), claim))
            self.assertFalse(await binding.verify_result({**SCOPE, "roomGeneration": 5}, claim))
            self.assertFalse(await binding.verify_result(dict(SCOPE), {**claim, "turn_id": "unknown-turn"}))
            self.assertFalse(await binding.verify_result(dict(SCOPE), {**claim, "speaker_ref": "qqg-ffffffffffffffff"}))
            self.assertFalse(await binding.verify_result(
                dict(SCOPE),
                {**claim, "uvc_session": {**claim["uvc_session"], "source_id": "secondary"}},
            ))
            self.assertEqual(set(binding._seen_turns.values()), {claim["speaker_ref"]})
            self.assertNotIn("anonymous-cluster", repr(binding._seen_turns))
            lease_current = False
            await wait_until(lambda: client.session.close_count == 1, timeout=2.0)
            self.assertFalse(await binding.verify_result(dict(SCOPE), claim))
            callback_release.set()
            await binding.close()

        self.assertEqual(client.session.close_count, 1)

    async def test_invalid_or_oversize_voice_base64_fails_closed(self) -> None:
        malformed = "%%%not-base64%%%"
        decoded_oversize = base64.b64encode(
            b"x" * (group_api._MAX_SPEECH_WAV_BYTES + 1)
        ).decode("ascii")
        encoded_oversize = "A" * (group_api._MAX_SPEECH_WAV_BASE64_CHARS + 1)
        self.assertLessEqual(len(decoded_oversize), group_api._MAX_SPEECH_WAV_BASE64_CHARS)
        for voice in (malformed, decoded_oversize, encoded_oversize):
            with self.subTest(size=len(voice)):
                binding, client, seen = self.make_binding()
                await binding.start(dict(SCOPE))
                await client.session.push(make_event(client.session, voice_base64=voice))
                await wait_until(lambda: client.session.close_count == 1)
                self.assertEqual(seen, [])
                self.assertEqual(binding.failure_code, "speech_result_invalid")

    async def test_asr_text_and_legacy_provenance_must_agree(self) -> None:
        binding, client, seen = self.make_binding()
        await binding.start(dict(SCOPE))
        await client.session.push(make_event(client.session, text="current", precomputed_asr_text="legacy"))
        await wait_until(lambda: client.session.close_count == 1)
        self.assertEqual(seen, [])
        self.assertEqual(binding.failure_code, "speech_result_invalid")

    async def test_asr_result_provenance_is_required_and_bounded(self) -> None:
        invalid_payloads = (
            {"precomputed_asr_result_id": ""},
            {"precomputed_asr_result_id": "x" * (group_api._MAX_ASR_RESULT_ID_BYTES + 1)},
            {"asr_scope": None},
            {"asr_scope": "x" * (group_api._MAX_ASR_SCOPE_BYTES + 1)},
        )
        for overrides in invalid_payloads:
            with self.subTest(overrides=tuple(overrides)):
                binding, client, seen = self.make_binding()
                await binding.start(dict(SCOPE))
                await client.session.push(make_event(client.session, **overrides))
                await wait_until(lambda: client.session.close_count == 1)
                self.assertEqual(seen, [])
                self.assertEqual(binding.failure_code, "speech_result_invalid")

    async def test_revoked_lease_drops_speech_result_without_log(self) -> None:
        checks = 0

        async def validator(_scope: dict[str, Any]) -> bool:
            nonlocal checks
            checks += 1
            return checks <= 2

        binding, client, seen = self.make_binding(validator=validator)
        await binding.start(dict(SCOPE))
        with patch.object(group_api._LOGGER, "info") as logger_info:
            await client.session.push(make_event(client.session))
            await wait_until(lambda: client.session.close_count == 1)
        self.assertEqual(seen, [])
        logger_info.assert_not_called()

    async def test_deduplication_budget_fails_closed_without_replaying_evicted_turn(self) -> None:
        binding, client, seen = self.make_binding()
        await binding.start(dict(SCOPE))
        session = client.session
        with patch.object(group_api, "_MAX_SEEN_TURNS", 2):
            await session.push(make_event(session, turn_id="one", speaker_id="a"))
            await session.push(make_event(session, turn_id="two", speaker_id="b"))
            await session.push(make_event(session, turn_id="one", speaker_id="a"))
            await session.push(make_event(session, turn_id="three", speaker_id="c"))
            await wait_until(lambda: session.close_count == 1)
        self.assertEqual(len(seen), 2)
        self.assertEqual([item["speech_result"]["turn_id"] for item in seen], ["one", "two"])
        self.assertEqual(binding.state, "failed")
        self.assertEqual(binding.failure_code, "deduplication_cap_reached")
        self.assertEqual(group_api._MAX_SEEN_TURNS, 8192)

    async def test_lease_monitor_cancels_inflight_pcm_send(self) -> None:
        lease_current = True
        session = FakeSession()
        session.send_gate = asyncio.Event()
        session.send_entered = asyncio.Event()

        async def validator(_scope: dict[str, Any]) -> bool:
            return lease_current

        binding, client, _ = self.make_binding(client=FakeClient(session), validator=validator)
        with patch.object(group_api, "_LEASE_MONITOR_INTERVAL", 0.01):
            await binding.start(dict(SCOPE))
            send_task = asyncio.create_task(binding.send_pcm(dict(SCOPE), b"\x01\x02"))
            await asyncio.wait_for(session.send_entered.wait(), timeout=1)
            lease_current = False
            await wait_until(lambda: session.close_count == 1, timeout=2.0)
            with self.assertRaises(asyncio.CancelledError):
                await send_task
        self.assertEqual(session.sent, [])
        self.assertEqual(binding.state, "failed")
        self.assertFalse(client.closed)

    async def test_close_drops_events_queued_after_reader_stops(self) -> None:
        binding, client, seen = self.make_binding()
        await binding.start(dict(SCOPE))
        await binding.close()
        await client.session.events_queue.put(make_event(client.session))
        await asyncio.sleep(0)
        self.assertEqual(seen, [])
        self.assertEqual(client.session.close_count, 1)
        self.assertEqual(binding.state, "closed")

    async def test_lease_monitor_cancels_reader_callback_and_send_within_bound(self) -> None:
        lease_current = True
        callback_started = asyncio.Event()
        callback_cancelled = asyncio.Event()
        session = FakeSession()
        session.send_gate = asyncio.Event()
        session.send_entered = asyncio.Event()

        async def callback(_envelope: dict[str, Any]) -> None:
            callback_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                callback_cancelled.set()
                raise

        async def validator(_scope: dict[str, Any]) -> bool:
            return lease_current

        binding, _client, _seen = self.make_binding(
            client=FakeClient(session), validator=validator, callback=callback
        )
        with patch.object(group_api, "_LEASE_MONITOR_INTERVAL", 0.01):
            await binding.start(dict(SCOPE))
            await session.push(make_event(session))
            await asyncio.wait_for(callback_started.wait(), timeout=1)
            send_task = asyncio.create_task(binding.send_pcm(dict(SCOPE), b"\x01\x02"))
            await asyncio.wait_for(session.send_entered.wait(), timeout=1)
            lease_current = False
            await asyncio.wait_for(callback_cancelled.wait(), timeout=2)
            await wait_until(lambda: session.close_count == 1, timeout=2.0)
            with self.assertRaises(asyncio.CancelledError):
                await send_task
            await wait_until(
                lambda: not any(
                    task.get_name() in {
                        "qqcall-group-lease-monitor",
                        "qqcall-group-universal-events",
                        "qqcall-group-late-session-cleanup",
                        "qqcall-group-session-cleanup",
                        "qqcall-group-session-close-call",
                    }
                    and not task.done()
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                ),
                timeout=2.0,
            )
        self.assertEqual(binding.state, "failed")
        self.assertEqual(binding._send_tasks, set())

    async def test_cancelled_start_drains_late_create_and_closes_returned_session(self) -> None:
        client = FakeClient()
        client.create_entered = asyncio.Event()
        client.create_release = asyncio.Event()
        client.ignore_create_cancellation = True
        binding, _, _ = self.make_binding(client=client)
        start_task = asyncio.create_task(binding.start(dict(SCOPE)))
        await asyncio.wait_for(client.create_entered.wait(), timeout=1)
        start_task.cancel()
        await asyncio.wait_for(asyncio.sleep(0), timeout=1)
        client.create_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await start_task
        await wait_until(lambda: client.session.close_count == 1)
        self.assertFalse(client.closed)

    async def test_close_is_idempotent_cancels_event_reader_and_keeps_client_open(self) -> None:
        binding, client, seen = self.make_binding()
        await binding.start(dict(SCOPE))
        await binding.close()
        await binding.close()
        await client.session.push(make_event(client.session))
        await asyncio.sleep(0)
        self.assertEqual(client.session.close_count, 1)
        self.assertEqual(seen, [])
        self.assertFalse(client.closed)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
