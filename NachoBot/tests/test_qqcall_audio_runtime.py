from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qqcall_client.qqcall_universal_api import QQCallRouteProof, QQCallVoiceRuntime  # noqa: E402


SCOPE = {
    "serviceInstance": "service-test",
    "hostGeneration": 1,
    "connectionGeneration": 2,
    "callId": "call-test",
    "callGeneration": 3,
}
CALL = {
    "scope": SCOPE,
    "peer_user_id": "12345",
    "peer_uid": "u_abcdefghijklmnopqrstuv",
    "phase": "connected",
    "uncertain": False,
    "inviteCallbackReady": True,
    "inviteCorrelation": "kernel_from_uid_matched",
}
IDENTITY = {"identity": (1, 2, "99999"), "account_id": "99999", "connected": True, "core_ready": True}


class FakeSession:
    def __init__(self):
        self.events_queue: asyncio.Queue[Any] = asyncio.Queue()
        self.frames: list[tuple[bytes, str]] = []
        self.interrupt_calls = 0
        self.close_calls = 0

    async def send_pcm(self, pcm: bytes, source_id: str = "main"):
        self.frames.append((pcm, source_id))
        return {"accepted": True}

    async def events(self):
        while True:
            value = await self.events_queue.get()
            if value is None:
                return
            yield value

    async def emit(self, event: str, turn_id: str, **extra: Any):
        await self.events_queue.put({
            "type": "event", "platform": "qq", "event": event,
            "source_id": "main", "turn_id": turn_id, **extra,
        })

    async def interrupt(self):
        self.interrupt_calls += 1
        return {"interrupted": True}

    async def close(self):
        self.close_calls += 1
        await self.events_queue.put(None)


class FakeClient:
    def __init__(self, *, gate: asyncio.Event | None = None, fail: bool = False):
        self.session = FakeSession()
        self.options: list[dict[str, Any]] = []
        self.create_started = asyncio.Event()
        self.gate = gate
        self.fail = fail
        self.close_calls = 0
        self.before_create_return = None

    async def create_session(self, options):
        self.options.append(options)
        self.create_started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("service down")
        if self.before_create_return is not None:
            self.before_create_return()
        return self.session

    async def close(self):
        self.close_calls += 1


class FakeBackend:
    def __init__(self, *, playback_gate: asyncio.Event | None = None):
        self.config = SimpleNamespace(capture_mode="loopback", capture_device="exact loopback", playback_device="exact virtual")
        self.status = {"state": "stopped", "capture_sample_counter": 0, "uplink_frames_sent": 0}
        self.start_calls = 0
        self.stop_calls = 0
        self.cancel_calls = 0
        self.played: list[tuple[str, Any]] = []
        self.send_pcm = None
        self.capture_fence = None
        self.playback_gate = playback_gate

    async def start(self, callback, on_failure, *, scope, send_pcm=None, capture_fence=None):
        self.start_calls += 1
        if callback is not None:
            raise AssertionError("audio binding must attach UniversalVC through the package uplink")
        self.send_pcm = send_pcm
        self.capture_fence = capture_fence
        if not callable(send_pcm) or not callable(capture_fence):
            raise AssertionError("missing injected UniversalVC uplink")
        self.status["state"] = "ready"

    async def emit_core_frame(self, pcm: bytes):
        if self.capture_fence() is not True:
            raise RuntimeError("stale fence")
        await self.send_pcm(pcm, source_id="main")

    async def play_core_segment(self, kind, data, fence):
        self.played.append((kind, data))
        if self.playback_gate is not None:
            await self.playback_gate.wait()
        if fence() is not True:
            raise RuntimeError("stale fence")
        return {"render_drained": True, "drain_confirmed": True}

    async def cancel_playback(self):
        self.cancel_calls += 1

    async def stop(self):
        self.stop_calls += 1
        self.status["state"] = "stopped"


async def _wait_until(predicate, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


class QQCallUniversalRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def make_runtime(self, *, route: bool = True, client: FakeClient | None = None, backend: FakeBackend | None = None):
        client = client or FakeClient()
        backend = backend or FakeBackend()
        made_clients: list[FakeClient] = []

        def make_client(_config):
            made_clients.append(client)
            return client

        runtime = QQCallVoiceRuntime(
            platform="qq",
            send_message=self._send_message,
            admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
            universal_api={"url": "http://127.0.0.1:8016", "token_file": "injected"},
            backend_factory=lambda _turn: backend,
            route_verifier=lambda _scope, _config: QQCallRouteProof(asyncio.get_running_loop().time()) if route else False,
            session_factory=make_client,
        )
        self.messages: list[Any] = []
        runtime._send_message = self._send_message
        return runtime, client, backend, made_clients

    async def _send_message(self, message):
        self.messages.append(message)
        return True

    async def start(self, runtime):
        runtime.update_call(CALL, IDENTITY)
        runtime.start_supervisor()
        await _wait_until(lambda: runtime.status()["ready"] or runtime.status()["lastFailureCode"] is not None)

    async def test_external_pipeline_options_and_uplink_are_bound_to_verified_scope(self):
        runtime, client, backend, made = self.make_runtime()
        await self.start(runtime)
        self.assertEqual(len(made), 1)
        options = client.options[0]
        self.assertEqual(options["mode"], "pipeline")
        self.assertEqual(options["sources"], [{
            "id": "main", "input": "external", "format": "s16le", "sample_rate": 16_000,
            "channels": 1, "capture": {},
            "identity": {"mode": "fixed", "id": "12345", "name": "12345"},
        }])
        self.assertEqual(options["output"], {"mode": "none"})
        self.assertEqual(options["scope"], hashlib.sha256(
            json.dumps(SCOPE, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest())
        self.assertTrue(backend.capture_fence())
        await backend.emit_core_frame(b"\x00\x00" * 320)
        self.assertEqual(client.session.frames, [(b"\x00\x00" * 320, "main")])
        await runtime.stop()

    async def test_speech_start_retires_old_reply_without_interrupting_new_asr(self):
        runtime, client, backend, _made = self.make_runtime()
        await self.start(runtime)
        await client.session.emit("speech_start", "turn-a")
        await _wait_until(lambda: runtime.current_turn_id(SCOPE) == "turn-a")
        await client.session.emit(
            "speech_result", "turn-a", voice_base64="complete-wav-a", text="hello",
            precomputed_asr_result_id="receipt-a", asr_scope="scope-from-core-a",
        )
        await _wait_until(lambda: len(self.messages) == 1)
        additional = self.messages[0].message_info.additional_config
        self.assertEqual(additional["precomputed_asr_scope"], "scope-from-core-a")
        self.assertEqual(additional["realtime_session"]["turn_id"], "turn-a")
        self.assertEqual(additional["qqcall_scope"], SCOPE)
        self.assertEqual(self.messages[0].message_info.user_info.user_id, "12345")
        await client.session.emit("speech_start", "turn-b")
        await _wait_until(lambda: runtime.current_turn_id(SCOPE) == "turn-b")
        await _wait_until(lambda: backend.cancel_calls > 0)
        self.assertEqual(client.session.interrupt_calls, 0)
        stale = await runtime.intercept_outbound(
            {"message_segment": {"type": "voice", "data": "old-reply"}},
            {**SCOPE, "peer_user_id": "12345", "turn_id": "turn-a"},
        )
        self.assertFalse(stale["accepted"])
        self.assertEqual(stale["code"], "stale_audio_turn")
        await client.session.emit("speech_result", "turn-a", voice_base64="stale-wav")
        await client.session.emit(
            "speech_result", "turn-b", voice_base64="complete-wav-b", text="next",
            precomputed_asr_result_id="receipt-b", asr_scope="scope-from-core-b",
        )
        await _wait_until(lambda: len(self.messages) == 2)
        duplicate_count = len(self.messages)
        await client.session.emit("speech_result", "turn-b", voice_base64="duplicate")
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.messages), duplicate_count)
        current = await runtime.intercept_outbound(
            {"message_segment": {"type": "voice", "data": "core-wav"}},
            {**SCOPE, "peer_user_id": "12345", "turn_id": "turn-b"},
        )
        self.assertTrue(current["accepted"])
        self.assertTrue(current["drain_confirmed"])
        self.assertEqual(backend.played, [("voice", "core-wav")])
        await runtime.stop()

    async def test_unverified_loopback_never_requests_service_or_opens_backend(self):
        client = FakeClient()
        backend = FakeBackend()
        made_backends: list[FakeBackend] = []

        def backend_factory(_turn):
            made_backends.append(backend)
            return backend

        made_clients: list[FakeClient] = []
        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=self._send_message, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
            universal_api={"url": "http://127.0.0.1:8016", "token_file": "injected"},
            backend_factory=backend_factory, route_verifier=lambda *_args: False,
            session_factory=lambda _cfg: made_clients.append(client) or client,
        )
        runtime.update_call(CALL, IDENTITY)
        runtime.start_supervisor()
        await _wait_until(lambda: runtime.status()["lastFailureCode"] is not None)
        self.assertEqual(runtime.status()["unavailableReason"], "universal_api_route_unverified")
        self.assertEqual(backend.start_calls, 0)
        self.assertEqual(client.options, [])
        self.assertEqual(made_clients, [])
        await runtime.stop()

    async def test_service_failure_blocks_scope_without_retry_and_closes_client(self):
        client = FakeClient(fail=True)
        runtime, _client, backend, made = self.make_runtime(client=client)
        await self.start(runtime)
        self.assertFalse(runtime.status()["ready"])
        self.assertEqual(runtime.status()["unavailableReason"], "universal_api_unavailable")
        await asyncio.sleep(0.4)
        self.assertEqual(len(made), 1)
        self.assertEqual(backend.start_calls, 0)
        self.assertEqual(client.close_calls, 1)
        await runtime.stop()

    async def test_close_during_create_closes_late_session(self):
        gate = asyncio.Event()
        client = FakeClient(gate=gate)
        runtime, _client, backend, _made = self.make_runtime(client=client)
        runtime.update_call(CALL, IDENTITY)
        runtime.start_supervisor()
        await asyncio.wait_for(client.create_started.wait(), 2)
        stopping = asyncio.create_task(runtime.stop())
        await _wait_until(lambda: bool(runtime._late_create_cleanup_tasks), 2)
        self.assertFalse(stopping.done(), "stop must wait for the owned late-create cleanup")
        gate.set()
        await asyncio.wait_for(stopping, 4)
        self.assertEqual(client.session.close_calls, 1)
        self.assertEqual(backend.start_calls, 0)

    async def test_cancel_at_create_completion_keeps_unpublished_session_owned(self):
        gate = asyncio.Event()
        client = FakeClient(gate=gate)
        runtime, _client, backend, _made = self.make_runtime(client=client)
        runtime.update_call(CALL, IDENTITY)
        startup = asyncio.create_task(
            runtime._create_session(runtime._desired, deadline=asyncio.get_running_loop().time() + 3.0)
        )
        await asyncio.wait_for(client.create_started.wait(), 2)
        create_task = runtime._create_task
        self.assertIsNotNone(create_task)
        self.assertIsNone(runtime._session)

        # Cancel the owner from inside the create coroutine immediately before
        # it returns. The native session exists, but the caller never publishes it.
        client.before_create_return = startup.cancel
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(startup, 3)

        self.assertTrue(create_task.done())
        self.assertIs(runtime._create_task, create_task)
        self.assertIsNone(runtime._session)
        self.assertEqual(client.session.close_calls, 0)
        await runtime._stop_resources(interrupt=True)
        await runtime.stop()

        self.assertEqual(client.session.close_calls, 1)
        self.assertEqual(client.close_calls, 1)
        self.assertEqual(backend.start_calls, 0)

    async def test_cancelled_late_create_failure_closes_client_without_retry(self):
        gate = asyncio.Event()
        client = FakeClient(gate=gate, fail=True)
        runtime, _client, backend, made = self.make_runtime(client=client)
        runtime.update_call(CALL, IDENTITY)
        startup = asyncio.create_task(
            runtime._create_session(runtime._desired, deadline=asyncio.get_running_loop().time() + 3.0)
        )
        await asyncio.wait_for(client.create_started.wait(), 2)
        startup.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(startup, 3)
        create_task = runtime._create_task
        self.assertIsNotNone(create_task)

        await runtime._stop_resources(interrupt=True)
        gate.set()
        await _wait_until(lambda: client.close_calls == 1)
        await runtime.stop()

        self.assertEqual(client.close_calls, 1)
        self.assertEqual(backend.start_calls, 0)
        self.assertEqual(len(made), 1)
        self.assertEqual(len(client.options), 1)

    async def test_stop_bounds_wait_and_retains_owner_for_still_pending_create(self):
        gate = asyncio.Event()
        client = FakeClient(gate=gate)
        runtime, _client, backend, _made = self.make_runtime(client=client)
        runtime.update_call(CALL, IDENTITY)
        startup = asyncio.create_task(
            runtime._create_session(runtime._desired, deadline=asyncio.get_running_loop().time() + 3.0)
        )
        await asyncio.wait_for(client.create_started.wait(), 2)
        startup.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(startup, 3)

        started = asyncio.get_running_loop().time()
        with patch("qqcall_client.qqcall_universal_api._CLEANUP_TIMEOUT", 0.02):
            await asyncio.wait_for(runtime.stop(), 1)
        elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, 0.2)
        self.assertTrue(runtime._late_create_cleanup_tasks)
        self.assertEqual(client.close_calls, 0)
        self.assertEqual(backend.start_calls, 0)

        gate.set()
        await _wait_until(lambda: client.close_calls == 1)
        self.assertEqual(client.session.close_calls, 1)
        await runtime.stop()

    async def test_route_revoke_during_core_playback_rejects_late_completion(self):
        gate = asyncio.Event()
        runtime, _client, backend, _made = self.make_runtime(backend=FakeBackend(playback_gate=gate))
        await self.start(runtime)
        client = runtime._client
        await client.session.emit("speech_start", "turn-live")
        await _wait_until(lambda: runtime.current_turn_id(SCOPE) == "turn-live")
        delivery = asyncio.create_task(runtime.intercept_outbound(
            {"message_segment": {"type": "voice", "data": "reply"}},
            {**SCOPE, "peer_user_id": "12345", "turn_id": "turn-live"},
        ))
        await asyncio.sleep(0.03)
        runtime._revoke_route_sync("universal_api_route_unverified")
        gate.set()
        result = await asyncio.wait_for(delivery, 2)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["code"], "stale_audio_turn")
        await runtime.stop()


if __name__ == "__main__":
    unittest.main()
