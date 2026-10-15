from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from typing import Any

from aiohttp import WSMsgType, web

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qqcall_client.qqcall_universal_api import (  # noqa: E402
    QQCallRouteProof,
    QQCallRouteRejection,
    QQCallRouteRetired,
    QQCallVoiceError,
    QQCallVoiceRuntime,
    _StartupScopeObsolete,
)
from universalvc_client import PROTOCOL_VERSION  # noqa: E402


_TOKEN = "qqcall-loopback-test-token"
_SCOPE = {
    "serviceInstance": "service-test",
    "hostGeneration": 1,
    "connectionGeneration": 2,
    "callId": "call-test",
    "callGeneration": 3,
}
_CALL = {
    "scope": _SCOPE,
    "peer_user_id": "12345",
    "peer_uid": "u_abcdefghijklmnopqrstuv",
    "phase": "connected",
    "uncertain": False,
    "inviteCallbackReady": True,
    "inviteCorrelation": "kernel_from_uid_matched",
}
_IDENTITY = {"identity": (1, 2, "99999"), "account_id": "99999", "connected": True, "core_ready": True}


class _LoopbackService:
    def __init__(self) -> None:
        self.runner: web.AppRunner | None = None
        self.base_url = ""
        self.options: list[dict[str, Any]] = []
        self.identities: dict[str, dict[str, Any]] = {}
        self.sockets: dict[str, web.WebSocketResponse] = {}
        self.requests: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self.frames: asyncio.Queue[tuple[str, dict[str, Any], bytes]] = asyncio.Queue()
        self.deleted: list[str] = []
        self.interrupted: list[str] = []
        app = web.Application()
        app.router.add_post("/v1/sessions", self.create)
        app.router.add_get("/v1/sessions/{session_id}/stream", self.stream)
        app.router.add_post("/v1/sessions/{session_id}/interrupt", self.interrupt)
        app.router.add_delete("/v1/sessions/{session_id}", self.delete)
        self.app = app

    def authorized(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {_TOKEN}"

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def create(self, request: web.Request) -> web.Response:
        if not self.authorized(request):
            return web.json_response({"error": {"code": "auth_required", "message": "token required"}}, status=401)
        value = await request.json()
        self.options.append(value)
        session_id = f"session-{len(self.options)}"
        self.identities[session_id] = {
            "protocol_version": PROTOCOL_VERSION,
            "service_instance_id": "test-service",
            "session_id": session_id,
            "session_generation": f"generation-{len(self.options)}",
        }
        return web.json_response({
            **self.identities[session_id],
            "websocket_path": f"/v1/sessions/{session_id}/stream",
        })

    async def stream(self, request: web.Request) -> web.StreamResponse:
        if not self.authorized(request):
            raise web.HTTPUnauthorized()
        session_id = request.match_info["session_id"]
        identity = self.identities[session_id]
        socket = web.WebSocketResponse(autoping=True)
        await socket.prepare(request)
        self.sockets[session_id] = socket
        await socket.send_json({"type": "ready", **identity})
        try:
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    value = json.loads(message.data)
                    if value.get("type") == "heartbeat":
                        continue
                    if value.get("type") == "request":
                        await self.requests.put((session_id, value))
                elif message.type == WSMsgType.BINARY:
                    raw = message.data
                    header_size = struct.unpack(">I", raw[:4])[0]
                    header = json.loads(raw[4:4 + header_size].decode("utf-8"))
                    pcm = raw[4 + header_size:]
                    await self.frames.put((session_id, header, pcm))
                    if header.get("op") == "send_pcm":
                        await socket.send_json({
                            "type": "response", **identity,
                            "request_id": header["request_id"], "ok": True,
                            "result": {"accepted": True, "source_id": header["source_id"]},
                        })
        finally:
            self.sockets.pop(session_id, None)
        return socket

    async def emit(self, session_id: str, event: str, **fields: Any) -> None:
        await self.sockets[session_id].send_json({
            "type": "event", **self.identities[session_id], "platform": "qq",
            "event": event, **fields,
        })

    async def disconnect(self, session_id: str) -> None:
        await self.sockets[session_id].close()

    async def interrupt(self, request: web.Request) -> web.Response:
        if not self.authorized(request):
            return web.json_response({"error": {"code": "auth_required", "message": "token required"}}, status=401)
        session_id = request.match_info["session_id"]
        self.interrupted.append(session_id)
        return web.json_response({"interrupted": True})

    async def delete(self, request: web.Request) -> web.Response:
        if not self.authorized(request):
            return web.json_response({"error": {"code": "auth_required", "message": "token required"}}, status=401)
        session_id = request.match_info["session_id"]
        self.deleted.append(session_id)
        return web.json_response({"closed": True})


class _FakeQQPCMDevice:
    """Test-only fake capture/sink exercising the QQCall driver seam."""

    def __init__(self) -> None:
        self.config = object()
        self.status = {"capture_sample_counter": 0, "uplink_frames_sent": 0}
        self.send_pcm = None
        self.capture_fence = None
        self.on_failure = None
        self.started = False
        self.stopped = False
        self.cancelled = 0
        self.played: list[tuple[str, Any]] = []

    async def start(self, callback, on_failure, *, scope, send_pcm, capture_fence) -> None:
        if callback is not None:
            raise AssertionError("UniversalVC input must be driven through the QQCall package uplink")
        self.send_pcm = send_pcm
        self.capture_fence = capture_fence
        self.on_failure = on_failure
        self.started = True

    async def inject_capture(self, data: bytes) -> Any:
        assert callable(self.capture_fence) and self.capture_fence() is True
        return await self.send_pcm(data, source_id="main")

    async def play_core_segment(self, kind: str, data: Any, fence) -> dict[str, Any]:
        if not fence():
            raise RuntimeError("stale playback fence")
        self.played.append((kind, data))
        if kind == "voice_stream" and isinstance(data, dict) and data.get("event") == "chunk":
            return {"queued": True, "drain_confirmed": False, "render_drained": False}
        return {"queued": True, "drain_confirmed": True, "render_drained": True}

    async def cancel_playback(self) -> None:
        self.cancelled += 1

    async def stop(self) -> None:
        self.stopped = True


class _StartupSession:
    def __init__(self, *, interrupt_gate=None, interrupt_started=None, close_gate=None, close_started=None) -> None:
        self.closed = False
        self.closing = False
        self.interrupted = False
        self.interrupt_gate = interrupt_gate
        self.interrupt_started = interrupt_started or asyncio.Event()
        self.close_gate = close_gate
        self.close_started = close_started or asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.interrupt_calls = 0
        self.close_calls = 0
        self.close_effects = 0

    async def send_pcm(self, *_args, **_kwargs):
        return {"accepted": True}

    async def events(self):
        while not self.closed:
            await asyncio.sleep(60)
            yield {"type": "heartbeat"}

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        self.interrupt_started.set()
        if self.interrupt_gate is not None:
            await self.interrupt_gate.wait()
        self.interrupted = True

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        if self.closed or self.closing:
            return
        self.closing = True
        if self.close_gate is not None:
            try:
                await self.close_gate.wait()
            except asyncio.CancelledError:
                self.close_cancelled.set()
                raise
        self.closed = True
        self.close_effects += 1


class _StartupClient:
    def __init__(self, *, create_gate: asyncio.Event | None = None, close_gate=None, close_started=None, session_factory=None) -> None:
        self.create_gate = create_gate
        self.close_gate = close_gate
        self.close_started = close_started or asyncio.Event()
        self.session_factory = session_factory or _StartupSession
        self.create_started = asyncio.Event()
        self.create_calls = 0
        self.session: _StartupSession | None = None
        self.closed = False
        self.close_effects = 0
        self.close_cancelled = asyncio.Event()

    async def create_session(self, _options):
        self.create_calls += 1
        self.create_started.set()
        if self.create_gate is not None:
            await self.create_gate.wait()
        self.session = self.session_factory()
        return self.session

    async def close(self) -> None:
        self.close_started.set()
        if self.close_gate is not None:
            try:
                await self.close_gate.wait()
            except asyncio.CancelledError:
                self.close_cancelled.set()
                raise
        if self.closed:
            return
        if self.session is not None:
            await self.session.close()
        self.closed = True
        self.close_effects += 1


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


class QQCallUniversalAPILoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = _LoopbackService()
        await self.service.start()

    async def asyncTearDown(self) -> None:
        await self.service.close()

    def test_route_rejection_transport_metadata_has_strict_bounded_shape(self) -> None:
        valid = QQCallRouteRejection(
            "route_helper_transport_error",
            _helper_http_status=503,
            _helper_error_code="service_stopped",
        )
        self.assertEqual(valid.reason, "route_helper_transport_error")
        self.assertEqual(valid._helper_http_status, 503)
        self.assertEqual(valid._helper_error_code, "service_stopped")

        for status in (True, False, 99, 600, 503.0, "503", object()):
            with self.subTest(status_type=type(status).__name__, status=status if type(status) in (int, str) else None):
                rejected = QQCallRouteRejection(
                    "route_helper_transport_error",
                    _helper_http_status=status,
                    _helper_error_code="call_not_connected",
                )
                self.assertIsNone(rejected._helper_http_status)
                self.assertEqual(rejected._helper_error_code, "call_not_connected")

        private_code = "u_private_caller_998877"
        unknown = QQCallRouteRejection(
            "route_helper_transport_error",
            _helper_http_status=401,
            _helper_error_code=private_code,
        )
        self.assertEqual(unknown._helper_http_status, 401)
        self.assertEqual(unknown._helper_error_code, "service_error_unclassified")
        self.assertNotIn(private_code, repr(unknown))

        missing = QQCallRouteRejection("route_helper_transport_error")
        self.assertIsNone(missing._helper_http_status)
        self.assertIsNone(missing._helper_error_code)
        unrelated = QQCallRouteRejection(
            "route_response_invalid",
            _helper_http_status=503,
            _helper_error_code="service_stopped",
        )
        self.assertIsNone(unrelated._helper_http_status)
        self.assertIsNone(unrelated._helper_error_code)

    def _new_startup_runtime(
        self,
        *,
        route_verifier,
        backend=None,
        backend_factory=None,
        client_factory=None,
        identity_is_current=None,
        admit_peer=None,
    ) -> QQCallVoiceRuntime:
        return QQCallVoiceRuntime(
            platform="qq",
            send_message=lambda _message: asyncio.sleep(0, result=True),
            admit_peer=admit_peer or (lambda _peer: True),
            identity_is_current=identity_is_current or (lambda _identity: True),
            backend=backend,
            backend_factory=backend_factory,
            route_verifier=route_verifier,
            session_factory=client_factory,
        )

    def test_pcm_backend_failure_callback_preserves_only_fixed_codes(self) -> None:
        def make_runtime(status_events):
            return QQCallVoiceRuntime(
                platform="qq",
                send_message=lambda _message: None,
                admit_peer=lambda _peer: True,
                identity_is_current=lambda _identity: True,
                backend_factory=lambda _turn: None,
                on_status=status_events.append,
            )

        string_events = []
        make_runtime(string_events)._backend_failed("capture_callback_failed")
        self.assertEqual(string_events[-1]["audio"]["lastFailureCode"], "capture_callback_failed")
        self.assertEqual(string_events[-1]["audio"]["unavailableReason"], "capture_callback_failed")

        object_events = []
        make_runtime(object_events)._backend_failed(SimpleNamespace(code="universal_api_protocol_invalid"))
        self.assertEqual(object_events[-1]["audio"]["lastFailureCode"], "universal_api_protocol_invalid")
        self.assertEqual(object_events[-1]["audio"]["unavailableReason"], "universal_api_protocol_invalid")

        private_values = ("u_private_caller_998877", "control-secret-value-41")
        for failure in (private_values[0], SimpleNamespace(code=private_values[1])):
            with self.subTest(failure_type=type(failure).__name__):
                events = []
                make_runtime(events)._backend_failed(failure)
                self.assertEqual(events[-1]["audio"]["lastFailureCode"], "audio_backend_failed")
                self.assertEqual(events[-1]["audio"]["unavailableReason"], "audio_backend_failed")
                self.assertFalse(any(secret in repr(events) for secret in private_values))

    async def test_frozen_sdk_session_pipeline_events_pcm_playback_drain_and_disconnect(self) -> None:
        backend = _FakeQQPCMDevice()
        messages = []

        async def send_message(message):
            messages.append(message)
            return True

        runtime = QQCallVoiceRuntime(
            platform="qq",
            send_message=send_message,
            admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
            universal_api={"url": self.service.base_url, "token_file": ""},
            backend_factory=lambda _turn: backend,
            route_verifier=lambda _scope, _config: True,
            resolve_peer_info=lambda peer: {"user_id": peer, "nickname": "真实昵称"},
        )
        with patch.dict(os.environ, {"NACHOBOT_UNIVERSALVC_TOKEN": _TOKEN}):
            runtime.update_call(_CALL, _IDENTITY)
            runtime.start_supervisor()
            await _wait_for(lambda: runtime.status()["ready"])
            self.assertEqual(len(self.service.options), 1)
            options = self.service.options[0]
            self.assertEqual(options["mode"], "pipeline")
            self.assertEqual(options["sources"][0]["input"], "external")
            self.assertEqual(options["sources"][0]["id"], "main")
            self.assertEqual(options["sources"][0]["identity"], {"mode": "fixed", "id": "12345", "name": "12345"})
            self.assertEqual(options["output"], {"mode": "none"})
            self.assertEqual(options["denoise"], False)
            self.assertEqual(options["speaker"], False)
            self.assertTrue(backend.started)

            result = await backend.inject_capture(b"\x01\x00" * 160)
            self.assertEqual(result, {"accepted": True, "source_id": "main"})
            session_id, request = await asyncio.wait_for(self.service.requests.get(), 2)
            frame_session, frame, pcm = await asyncio.wait_for(self.service.frames.get(), 2)
            self.assertEqual(session_id, frame_session)
            self.assertEqual(request["op"], "send_pcm")
            self.assertEqual(frame["request_id"], request["request_id"])
            self.assertEqual(frame["source_id"], "main")
            self.assertEqual(frame["seq"], 0)
            self.assertEqual(pcm, b"\x01\x00" * 160)

            await self.service.emit(session_id, "speech_start", source_id="main", turn_id="turn-1")
            await _wait_for(lambda: runtime.current_turn_id(_SCOPE) == "turn-1")
            wav = base64.b64encode(b"RIFF-frozen-api-result").decode("ascii")
            await self.service.emit(
                session_id, "speech_result", source_id="main", turn_id="turn-1",
                voice_base64=wav, text="recognized", precomputed_asr_result_id="receipt-1",
                asr_scope="core-receipt-scope",
            )
            await _wait_for(lambda: len(messages) == 1)
            incoming = messages[0]
            self.assertEqual(incoming.message_info.user_info.user_nickname, "真实昵称")
            self.assertEqual(incoming.message_info.additional_config["runtime_capabilities"]["tts_language"], "zh")
            self.assertIs(incoming.message_info.additional_config["runtime_capabilities"]["mcp_enabled"], False)
            self.assertEqual(incoming.message_segment.type, "voice")
            self.assertEqual(incoming.message_segment.data, wav)
            self.assertEqual(incoming.message_info.additional_config["precomputed_asr_scope"], "core-receipt-scope")
            self.assertEqual(incoming.message_info.additional_config["realtime_session"]["turn_id"], "turn-1")

            chunk = {"event": "chunk", "stream_id": "stream-1", "seq": 0, "audio_base64": "AA=="}
            receipt = await runtime.intercept_outbound(
                {"message_segment": {"type": "voice_stream", "data": chunk}},
                {**_SCOPE, "peer_user_id": "12345", "turn_id": "turn-1"},
            )
            self.assertTrue(receipt["accepted"])
            self.assertFalse(receipt["drain_confirmed"])
            end = {"event": "end", "stream_id": "stream-1", "seq": 1}
            drained = await runtime.intercept_outbound(
                {"message_segment": {"type": "voice_stream", "data": end}},
                {**_SCOPE, "peer_user_id": "12345", "turn_id": "turn-1"},
            )
            self.assertTrue(drained["drain_confirmed"])
            self.assertEqual([kind for kind, _ in backend.played], ["voice_stream", "voice_stream"])

            await self.service.disconnect(session_id)
            await _wait_for(lambda: runtime.status()["unavailableReason"] == "universal_api_disconnected")
            self.assertGreater(backend.cancelled, 0)
            await runtime.stop()
            self.assertTrue(backend.stopped)
            self.assertEqual(self.service.interrupted, [session_id])
            self.assertEqual(self.service.deleted, [session_id])

    async def test_peer_nickname_rejects_foreign_profile_retries_failure_and_caches_verified_name(self) -> None:
        backend = _FakeQQPCMDevice()
        messages = []
        lookups = []
        async def resolve(peer):
            lookups.append(peer)
            if len(lookups) == 1:
                return {"user_id": "54321", "nickname": "别人的昵称"}
            if len(lookups) == 2:
                raise RuntimeError("lookup unavailable")
            return {"user_id": peer, "nickname": "真实昵称"}
        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda message: messages.append(message) or asyncio.sleep(0, result=True),
            admit_peer=lambda _peer: True, identity_is_current=lambda _identity: True,
            universal_api={"url": self.service.base_url, "token_file": ""},
            backend_factory=lambda _turn: backend, route_verifier=lambda _scope, _config: True,
            resolve_peer_info=resolve,
        )
        with patch.dict(os.environ, {"NACHOBOT_UNIVERSALVC_TOKEN": _TOKEN}):
            try:
                runtime.update_call(_CALL, _IDENTITY)
                runtime.start_supervisor()
                await _wait_for(lambda: runtime.status()["ready"])
                session_id = self.service.options[0]["session_id"] if "session_id" in self.service.options[0] else next(iter(self.service.sockets))
                for index in range(4):
                    turn = f"profile-turn-{index}"
                    await self.service.emit(session_id, "speech_start", source_id="main", turn_id=turn)
                    await _wait_for(lambda: runtime.current_turn_id(_SCOPE) == turn)
                    await self.service.emit(session_id, "speech_result", source_id="main", turn_id=turn,
                        voice_base64=base64.b64encode(b"RIFF-profile-test").decode("ascii"))
                    await _wait_for(lambda: len(messages) == index + 1)
                self.assertEqual([message.message_info.user_info.user_nickname for message in messages],
                                 ["12345", "12345", "真实昵称", "真实昵称"])
                self.assertEqual(lookups, ["12345"] * 3)
            finally:
                await runtime.stop()

    async def test_delayed_monitor_proof_does_not_block_speech_events_and_reports_bounded_timing(self) -> None:
        backend = _FakeQQPCMDevice()
        backend.status.update(capture_sample_counter=1600, uplink_frames_sent=6)
        messages = []
        monitor_entered = asyncio.Event()
        release_monitor = asyncio.Event()
        route_calls = 0

        async def verify_route(_scope, _config):
            nonlocal route_calls
            route_calls += 1
            if route_calls > 2:
                monitor_entered.set()
                await release_monitor.wait()
            return True

        runtime = QQCallVoiceRuntime(
            platform="qq",
            send_message=lambda message: messages.append(message) or asyncio.sleep(0, result=True),
            admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
            universal_api={"url": self.service.base_url, "token_file": ""},
            backend_factory=lambda _turn: backend,
            route_verifier=verify_route,
        )
        with patch.dict(os.environ, {"NACHOBOT_UNIVERSALVC_TOKEN": _TOKEN}):
            runtime.update_call(_CALL, _IDENTITY)
            runtime.start_supervisor()
            try:
                await _wait_for(lambda: runtime.status()["ready"])
                await asyncio.wait_for(monitor_entered.wait(), timeout=2)
                session_id = next(iter(self.service.sockets))
                await self.service.emit(session_id, "speech_start", source_id="main", turn_id="delayed-turn")
                await _wait_for(lambda: runtime.current_turn_id(_SCOPE) == "delayed-turn")
                wav = base64.b64encode(b"RIFF-delayed-monitor-result").decode("ascii")
                await self.service.emit(
                    session_id, "speech_result", source_id="main", turn_id="delayed-turn",
                    voice_base64=wav, text="recognized", precomputed_asr_result_id="receipt-delayed",
                    asr_scope="core-receipt-scope",
                )
                await _wait_for(lambda: len(messages) == 1)
                self.assertTrue(release_monitor.is_set() is False)

                release_monitor.set()
                await _wait_for(lambda: runtime.status()["routeDiagnostics"]["monitorChecks"] >= 1)
                status = runtime.status()
                diagnostics = status["routeDiagnostics"]
                self.assertGreater(diagnostics["monitorStepMs"], 0)
                self.assertGreaterEqual(diagnostics["proofAgeMs"], -250)
                self.assertEqual(diagnostics["firstFailure"], None)
                self.assertEqual(status["framesReceived"], 1600)
                self.assertEqual(status["framesSent"], 6)
            finally:
                release_monitor.set()
                await runtime.stop()

    def test_route_refresh_delay_tracks_proof_age_and_has_bounded_retry(self) -> None:
        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
        )
        with patch("qqcall_client.qqcall_universal_api.time.monotonic", return_value=100.0):
            runtime._route_verified_at = 100.0
            self.assertEqual(runtime._route_refresh_delay(), 0.25)
            runtime._route_verified_at = 99.9
            self.assertAlmostEqual(runtime._route_refresh_delay(), 0.15)
            runtime._route_verified_at = 98.8
            self.assertEqual(runtime._route_refresh_delay(), 0.05)
            runtime._route_verified_at = 100.1
            self.assertEqual(runtime._route_refresh_delay(), 0.25)

    async def test_route_monitor_refreshes_aged_cached_proof_before_capture_fence_expires(self) -> None:
        old_proof_at = time.monotonic() - 1.5
        refresh_entered = asyncio.Event()
        release_refresh = asyncio.Event()
        route_calls = 0

        async def verify_route(_scope, _config):
            nonlocal route_calls
            route_calls += 1
            if route_calls == 1:
                # Model the observer's still-valid cache returning its prior
                # actual proof timestamp; a cache hit must not refresh age.
                return QQCallRouteProof(old_proof_at)
            refresh_entered.set()
            await release_refresh.wait()
            return QQCallRouteProof(time.monotonic())

        async def identity_is_current(_identity):
            await asyncio.sleep(0.005)
            return True

        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=identity_is_current, route_verifier=verify_route,
        )
        runtime.update_call(_CALL, _IDENTITY)
        desired = runtime._desired
        session = object()
        runtime._session = session
        runtime._active_scope_key = desired.scope_key
        runtime._ready = True
        runtime._capture_enabled = True
        runtime._route_verified_at = old_proof_at
        monitor = asyncio.create_task(runtime._route_monitor(desired))
        try:
            await asyncio.wait_for(refresh_entered.wait(), timeout=2.5)
            self.assertEqual(route_calls, 2)
            self.assertTrue(runtime._capture_transport_fence(desired, session))
            release_refresh.set()
            await _wait_for(lambda: runtime.status()["routeDiagnostics"]["monitorChecks"] >= 2)
            self.assertTrue(runtime._route_is_fresh())
            self.assertTrue(runtime._capture_transport_fence(desired, session))
            self.assertIsNone(runtime.status()["routeDiagnostics"]["firstFailure"])

            # The same capture fence still rejects a genuinely expired proof.
            runtime._route_verified_at = time.monotonic() - 2.501
            self.assertFalse(runtime._capture_transport_fence(desired, session))
            self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailure"], "route_proof_stale")
        finally:
            release_refresh.set()
            runtime._ready = False
            if not monitor.done():
                await asyncio.wait_for(monitor, timeout=1.0)

    async def test_route_monitor_rejects_proof_that_expires_during_identity_recheck(self) -> None:
        identity_checks = 0

        async def identity_is_current(_identity):
            nonlocal identity_checks
            identity_checks += 1
            if identity_checks == 2:
                await asyncio.sleep(0.35)
            return True

        async def verify_route(_scope, _config):
            return QQCallRouteProof(time.monotonic() - 2.3)

        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=identity_is_current, route_verifier=verify_route,
        )
        runtime.update_call(_CALL, _IDENTITY)
        desired = runtime._desired
        session = object()
        runtime._session = session
        runtime._active_scope_key = desired.scope_key
        runtime._ready = True
        runtime._capture_enabled = True
        runtime._route_verified_at = time.monotonic() - 1.5

        await runtime._route_monitor(desired)

        status = runtime.status()
        self.assertEqual(status["routeDiagnostics"]["firstFailure"], "route_proof_stale")
        self.assertEqual(status["lastFailureCode"], "audio_scope_stale")
        self.assertFalse(runtime._capture_transport_fence(desired, session))

    async def test_identity_false_and_stale_proof_have_distinct_first_failure_reasons(self) -> None:
        identity_runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: False,
        )
        identity_runtime.update_call(_CALL, _IDENTITY)
        self.assertFalse(await identity_runtime._turn_current(identity_runtime._desired))
        self.assertEqual(
            identity_runtime.status()["routeDiagnostics"]["firstFailure"], "identity_check_false"
        )

        stale_runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
        )
        stale_runtime.update_call(_CALL, _IDENTITY)
        desired = stale_runtime._desired

        class _OneEventSession:
            async def events(self):
                yield {"type": "event"}

        session = _OneEventSession()
        stale_runtime._session = session
        stale_runtime._active_scope_key = desired.scope_key
        stale_runtime._ready = True
        stale_runtime._capture_enabled = True
        stale_runtime._route_verified_at = time.monotonic() - 3.0
        await stale_runtime._events_worker(desired, session)
        status = stale_runtime.status()
        self.assertEqual(status["routeDiagnostics"]["firstFailure"], "route_proof_stale")
        self.assertEqual(status["lastFailureCode"], "audio_scope_stale")

    async def test_identity_timeout_and_scope_identity_mismatches_are_classified_without_exposure(self) -> None:
        async def wait_forever(_identity):
            await asyncio.Event().wait()

        timeout_runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=wait_forever,
        )
        timeout_runtime.update_call(_CALL, _IDENTITY)
        with patch("qqcall_client.qqcall_universal_api._ROUTE_CHECK_TIMEOUT", 0.01):
            self.assertFalse(await timeout_runtime._turn_current(timeout_runtime._desired))
        self.assertEqual(
            timeout_runtime.status()["routeDiagnostics"]["firstFailure"], "identity_check_timeout"
        )

        identity_runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
        )
        identity_runtime.update_call(_CALL, _IDENTITY)
        old_turn = identity_runtime._desired
        replacement_identity = {**_IDENTITY, "account_id": "77777"}
        identity_runtime.update_call(_CALL, replacement_identity)
        self.assertFalse(await identity_runtime._turn_current(old_turn))
        self.assertEqual(
            identity_runtime.status()["routeDiagnostics"]["firstFailure"], "desired_identity_mismatch"
        )

        scope_runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
        )
        scope_runtime.update_call(_CALL, _IDENTITY)
        old_turn = scope_runtime._desired
        replacement_call = {**_CALL, "scope": {**_SCOPE, "callGeneration": 4}}
        scope_runtime.update_call(replacement_call, _IDENTITY)
        self.assertFalse(await scope_runtime._turn_current(old_turn))
        diagnostics = scope_runtime.status()["routeDiagnostics"]
        self.assertIsNone(diagnostics["firstFailure"])
        self.assertEqual(diagnostics["failureCount"], 0)
        self.assertEqual(diagnostics["identityChecks"], 0)

    async def test_old_scope_route_monitor_completion_cannot_write_new_scope_diagnostics(self) -> None:
        verifier_started = asyncio.Event()
        release_verifier = asyncio.Event()

        async def delayed_rejection(_scope, _config):
            verifier_started.set()
            await release_verifier.wait()
            return QQCallRouteRejection("host_input_not_active")

        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True, route_verifier=delayed_rejection,
        )
        runtime.update_call(_CALL, _IDENTITY)
        old_turn = runtime._desired
        runtime._active_scope_key = old_turn.scope_key
        runtime._ready = True
        runtime._capture_enabled = True
        runtime._route_verified_at = time.monotonic()

        async def no_wait(_delay):
            return None

        with patch("qqcall_client.qqcall_universal_api.asyncio.sleep", no_wait):
            old_monitor = asyncio.create_task(runtime._route_monitor(old_turn))
            await asyncio.wait_for(verifier_started.wait(), timeout=1)
            next_call = {**_CALL, "scope": {**_SCOPE, "callGeneration": 4}}
            runtime.update_call(next_call, _IDENTITY)
            release_verifier.set()
            await old_monitor

        diagnostics = runtime.status()["routeDiagnostics"]
        self.assertIsNone(diagnostics["firstFailure"])
        self.assertEqual(diagnostics["failureCount"], 0)
        self.assertEqual(diagnostics["identityChecks"], 0)
        self.assertEqual(diagnostics["monitorChecks"], 0)
        self.assertIsNone(diagnostics["monitorStepMs"])

        # The currently bound scope can still record its own route failure.
        runtime._route_verifier = lambda _scope, _config: QQCallRouteRejection("observer_timeout")
        with self.assertRaises(QQCallVoiceError):
            await runtime._verify_route(runtime._desired)
        self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailure"], "observer_timeout")
        self.assertEqual(runtime.status()["routeDiagnostics"]["failureCount"], 1)

    async def test_old_scope_identity_and_capture_fence_diagnostics_are_ignored(self) -> None:
        identity_started = asyncio.Event()
        release_identity = asyncio.Event()

        async def delayed_identity_failure(_identity):
            identity_started.set()
            await release_identity.wait()
            return False

        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=delayed_identity_failure,
        )
        runtime.update_call(_CALL, _IDENTITY)
        old_turn = runtime._desired
        old_check = asyncio.create_task(runtime._turn_current(old_turn))
        await asyncio.wait_for(identity_started.wait(), timeout=1)

        next_call = {**_CALL, "scope": {**_SCOPE, "callGeneration": 4}}
        runtime.update_call(next_call, _IDENTITY)
        release_identity.set()
        self.assertFalse(await old_check)

        session = object()
        runtime._session = session
        runtime._capture_enabled = True
        self.assertFalse(runtime._capture_transport_fence(old_turn, session))

        diagnostics = runtime.status()["routeDiagnostics"]
        self.assertIsNone(diagnostics["firstFailure"])
        self.assertEqual(diagnostics["failureCount"], 0)
        self.assertEqual(diagnostics["identityChecks"], 0)
        self.assertEqual(diagnostics["monitorChecks"], 0)
        self.assertIsNone(diagnostics["proofAgeMs"])

    async def test_route_monitor_records_fixed_verifier_error_and_elapsed_step(self) -> None:
        def broken_verifier(_scope, _config):
            raise RuntimeError("private verifier detail must not escape")

        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True, route_verifier=broken_verifier,
        )
        runtime.update_call(_CALL, _IDENTITY)
        desired = runtime._desired
        runtime._active_scope_key = desired.scope_key
        runtime._ready = True
        runtime._capture_enabled = True
        runtime._route_verified_at = time.monotonic()

        async def no_wait(_delay):
            return None

        with patch("qqcall_client.qqcall_universal_api.asyncio.sleep", no_wait):
            await runtime._route_monitor(desired)
        diagnostics = runtime.status()["routeDiagnostics"]
        self.assertEqual(diagnostics["firstFailure"], "route_verifier_error")
        self.assertFalse(runtime.status()["ready"])
        self.assertFalse(runtime.status()["capturing"])
        self.assertEqual(runtime._failure_code, "universal_api_route_unverified")
        self.assertEqual(diagnostics["failureCount"], 1)
        self.assertEqual(diagnostics["monitorChecks"], 1)
        self.assertGreaterEqual(diagnostics["monitorStepMs"], 0)
        self.assertNotIn("private verifier detail", repr(diagnostics))

    async def test_route_monitor_discards_obsolete_verifier_outcomes(self) -> None:
        cases = (
            ("proof", "terminal"),
            ("rejection", "terminal"),
            ("exception", "replacement"),
            ("timeout", "replacement"),
        )
        for outcome, transition in cases:
            with self.subTest(outcome=outcome, transition=transition):
                verifier_entered = asyncio.Event()
                release_verifier = asyncio.Event()

                async def verify_route(
                    _scope,
                    _config,
                    *,
                    case_outcome=outcome,
                    case_verifier_entered=verifier_entered,
                    case_release_verifier=release_verifier,
                ):
                    case_verifier_entered.set()
                    if case_outcome == "timeout":
                        await asyncio.Event().wait()
                    await case_release_verifier.wait()
                    if case_outcome == "exception":
                        raise RuntimeError("obsolete verifier detail")
                    if case_outcome == "rejection":
                        return QQCallRouteRejection("observer_timeout")
                    return QQCallRouteProof(time.monotonic())

                runtime = QQCallVoiceRuntime(
                    platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
                    identity_is_current=lambda _identity: True, route_verifier=verify_route,
                )
                runtime.update_call(_CALL, _IDENTITY)
                old_turn = runtime._desired
                runtime._active_scope_key = old_turn.scope_key
                runtime._ready = True
                runtime._capture_enabled = True
                runtime._route_verified_at = time.monotonic()
                runtime._route_refresh_delay = lambda: 0.0
                timeout_patch = patch("qqcall_client.qqcall_universal_api._ROUTE_VERIFIER_TIMEOUT", 0.03)
                if outcome == "timeout":
                    timeout_patch.start()
                monitor = asyncio.create_task(runtime._route_monitor(old_turn))
                try:
                    await asyncio.wait_for(verifier_entered.wait(), timeout=0.5)
                    new_turn = None
                    if transition == "terminal":
                        runtime.update_call(None, None)
                    else:
                        replacement = {**_CALL, "scope": {**_SCOPE, "callGeneration": 8}}
                        runtime.update_call(replacement, _IDENTITY)
                        new_turn = runtime._desired
                        runtime._active_scope_key = new_turn.scope_key
                        runtime._ready = True
                        runtime._capture_enabled = True
                        runtime._route_verified_at = time.monotonic()

                    if outcome == "timeout":
                        await asyncio.wait_for(monitor, timeout=0.5)
                    else:
                        release_verifier.set()
                        await asyncio.wait_for(monitor, timeout=0.5)

                    diagnostics = runtime.status()["routeDiagnostics"]
                    self.assertIsNone(diagnostics["firstFailure"])
                    self.assertEqual(diagnostics["failureCount"], 0)
                    if new_turn is not None:
                        self.assertTrue(runtime.status()["ready"])
                        self.assertTrue(runtime.status()["capturing"])
                        self.assertEqual(runtime._active_scope_key, new_turn.scope_key)
                        self.assertIsNone(runtime._failure_code)
                    else:
                        self.assertFalse(runtime.status()["ready"])
                        self.assertEqual(runtime._unavailable_reason, "call_not_connected")
                        self.assertIsNone(runtime._failure_code)
                        self.assertFalse(runtime._route_failed)
                finally:
                    release_verifier.set()
                    if not monitor.done():
                        monitor.cancel()
                        await asyncio.gather(monitor, return_exceptions=True)
                    if outcome == "timeout":
                        timeout_patch.stop()
                    await runtime.stop()

    async def test_route_monitor_revoke_is_atomic_with_cross_thread_call_transition(self) -> None:
        transitions = ("terminal", "same_scope_replacement", "new_scope_replacement")
        for transition in transitions:
            with self.subTest(transition=transition):
                checked_for_revoke = threading.Event()
                release_revoke = threading.Event()
                transition_finished = threading.Event()
                worker_errors: list[BaseException] = []
                status_events: list[dict[str, Any]] = []
                transition_diagnostics: list[dict[str, Any]] = []

                class Backend:
                    def __init__(self) -> None:
                        self.cancel_count = 0

                    async def cancel_playback(self) -> None:
                        self.cancel_count += 1

                backend = Backend()
                runtime = QQCallVoiceRuntime(
                    platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
                    identity_is_current=lambda _identity: True, backend=backend,
                    on_status=status_events.append,
                )
                runtime._loop = asyncio.get_running_loop()
                runtime.update_call(_CALL, _IDENTITY)
                old_turn = runtime._desired
                runtime._active_scope_key = old_turn.scope_key
                runtime._ready = True
                runtime._capture_enabled = True
                runtime._route_verified_at = time.monotonic()
                runtime._blocked_scope_key = ("prior-block",)
                runtime._route_refresh_delay = lambda: 0.0

                async def fail_turn_check(_desired) -> bool:
                    return False

                runtime._turn_current = fail_turn_check

                original_check = runtime._route_check_current
                check_count = [0]

                def pause_after_final_check(
                    desired,
                    _original_check=original_check,
                    _checked_for_revoke=checked_for_revoke,
                    _release_revoke=release_revoke,
                    _check_count=check_count,
                ) -> bool:
                    current = _original_check(desired)
                    if current:
                        _check_count[0] += 1
                        # The first check starts a monitor pass; the second is
                        # the old check/revoke gap after a failing proof.
                        if _check_count[0] == 2:
                            _checked_for_revoke.set()
                            if not _release_revoke.wait(timeout=1.0):
                                raise AssertionError("timed out waiting to release monitor revoke barrier")
                    return current

                runtime._route_check_current = pause_after_final_check
                monitor_errors: list[BaseException] = []
                monitor_finished = threading.Event()

                def run_monitor(
                    _runtime=runtime,
                    _old_turn=old_turn,
                    _monitor_errors=monitor_errors,
                    _monitor_finished=monitor_finished,
                ) -> None:
                    async def run() -> None:
                        _runtime._loop = asyncio.get_running_loop()
                        await _runtime._route_monitor(_old_turn)
                        # Give any erroneous unguarded revoke callback a chance
                        # to run before the owned loop closes.
                        await asyncio.sleep(0)

                    try:
                        asyncio.run(run())
                    except BaseException as exc:
                        _monitor_errors.append(exc)
                    finally:
                        _monitor_finished.set()

                def transition_call(
                    _runtime=runtime,
                    _transition=transition,
                    _worker_errors=worker_errors,
                    _transition_diagnostics=transition_diagnostics,
                    _transition_finished=transition_finished,
                ) -> None:
                    try:
                        if _transition == "terminal":
                            _runtime.update_call(None, None)
                            # Complete the terminal teardown state that the
                            # supervisor applies after desired-call removal.
                            _runtime._capture_enabled = False
                        elif _transition == "same_scope_replacement":
                            refreshed_identity = {
                                **_IDENTITY,
                                "identity": (1, 2, "refreshed"),
                            }
                            _runtime.update_call(_CALL, refreshed_identity)
                        else:
                            replacement = {**_CALL, "scope": {**_SCOPE, "callGeneration": 8}}
                            _runtime.update_call(replacement, _IDENTITY)

                        if _transition != "terminal":
                            current_turn = _runtime._desired
                            _runtime._active_scope_key = current_turn.scope_key
                            _runtime._ready = True
                            _runtime._capture_enabled = True
                            _runtime._route_verified_at = time.monotonic()
                            _runtime._failure_code = None
                            _runtime._unavailable_reason = ""
                        _transition_diagnostics.append(_runtime._route_diagnostics_snapshot())
                    except BaseException as exc:
                        _worker_errors.append(exc)
                    finally:
                        _transition_finished.set()

                monitor_thread = threading.Thread(target=run_monitor, daemon=True)
                monitor_thread.start()
                try:
                    self.assertTrue(
                        await asyncio.to_thread(checked_for_revoke.wait, 1.0),
                        f"monitor did not reach the check/revoke boundary (checks={check_count[0]}, errors={monitor_errors})",
                    )
                    worker = threading.Thread(target=transition_call, daemon=True)
                    worker.start()
                    self.assertTrue(
                        await asyncio.to_thread(transition_finished.wait, 1.0),
                        "cross-thread update_call did not finish",
                    )
                    worker.join(timeout=1.0)
                    self.assertFalse(worker.is_alive(), "cross-thread update_call worker remained alive")
                    self.assertEqual(worker_errors, [])
                    release_revoke.set()
                    self.assertTrue(await asyncio.to_thread(monitor_finished.wait, 1.0))
                    monitor_thread.join(timeout=1.0)
                    self.assertFalse(monitor_thread.is_alive(), "route monitor worker remained alive")
                    self.assertEqual(monitor_errors, [])

                    self.assertEqual(len(transition_diagnostics), 1)
                    self.assertEqual(runtime._route_diagnostics_snapshot(), transition_diagnostics[0])
                    self.assertFalse(runtime._route_failed)
                    self.assertEqual(runtime._blocked_scope_key, ("prior-block",))
                    self.assertEqual(runtime._failure_code, None)
                    self.assertEqual(backend.cancel_count, 0)
                    self.assertEqual(status_events, [])
                    if transition == "terminal":
                        self.assertIsNone(runtime._desired)
                        self.assertFalse(runtime._ready)
                        self.assertFalse(runtime._capture_enabled)
                        self.assertEqual(runtime._unavailable_reason, "call_not_connected")
                    else:
                        self.assertIsNotNone(runtime._desired)
                        self.assertTrue(runtime._ready)
                        self.assertTrue(runtime._capture_enabled)
                        self.assertEqual(runtime._unavailable_reason, "")
                        if transition == "same_scope_replacement":
                            self.assertEqual(runtime._desired.scope_key, old_turn.scope_key)
                            self.assertNotEqual(runtime._desired.identity, old_turn.identity)
                        else:
                            self.assertNotEqual(runtime._desired.scope_key, old_turn.scope_key)
                finally:
                    release_revoke.set()
                    monitor_thread.join(timeout=1.0)
                    if monitor_thread.is_alive():
                        self.fail("route monitor worker did not stop")
                    await runtime.stop()

    async def test_exact_scope_retirement_cleans_A_then_late_A_work_cannot_touch_B(self) -> None:
        backends: list[_FakeQQPCMDevice] = []
        clients: list[_StartupClient] = []
        route_calls: dict[int, int] = {}

        def backend_factory(_turn):
            backend = _FakeQQPCMDevice()
            backends.append(backend)
            return backend

        def client_factory(_api):
            client = _StartupClient()
            clients.append(client)
            return client

        def verify_route(scope, _config):
            call_generation = scope["callGeneration"]
            route_calls[call_generation] = route_calls.get(call_generation, 0) + 1
            if call_generation == _SCOPE["callGeneration"] and route_calls[call_generation] > 2:
                return QQCallRouteRetired()
            return QQCallRouteProof(time.monotonic())

        runtime = self._new_startup_runtime(
            route_verifier=verify_route,
            backend_factory=backend_factory,
            client_factory=client_factory,
        )
        runtime.update_call(_CALL, _IDENTITY)
        turn_a = runtime._desired
        scope_a_key = turn_a.scope_key
        runtime._route_refresh_delay = lambda: (
            0.001 if runtime._active_scope_key == scope_a_key else 60.0
        )
        runtime.start_supervisor()
        try:
            await _wait_for(
                lambda: runtime._retired_scope_key == scope_a_key
                and len(backends) == 1 and backends[0].stopped
                and len(clients) == 1 and clients[0].closed
            )
            session_a = clients[0].session
            self.assertIsNotNone(session_a)
            self.assertTrue(session_a.closed)
            self.assertEqual(runtime._blocked_scope_key, scope_a_key)
            self.assertIsNone(runtime._desired)
            retired_status = runtime.status()
            self.assertFalse(retired_status["ready"])
            self.assertFalse(retired_status["capturing"])
            self.assertEqual(retired_status["unavailableReason"], "call_not_connected")
            self.assertIsNone(retired_status["lastFailureCode"])
            self.assertFalse(runtime._route_failed)
            self.assertIsNone(retired_status["routeDiagnostics"]["firstFailure"])
            self.assertEqual(retired_status["routeDiagnostics"]["failureCount"], 0)

            # A lagging connected poll for the same retired Bridge scope may
            # not clear its single marker or restart its capture pipeline.
            for _ in range(2):
                runtime.update_call(_CALL, _IDENTITY)
                await asyncio.sleep(0.01)
            self.assertEqual(runtime._retired_scope_key, scope_a_key)
            self.assertIsNone(runtime._desired)
            self.assertEqual(len(backends), 1)
            self.assertEqual(len(clients), 1)
            self.assertEqual(runtime._unavailable_reason, "call_not_connected")

            call_b = {**_CALL, "scope": {**_SCOPE, "callGeneration": _SCOPE["callGeneration"] + 1}}
            runtime.update_call(call_b, _IDENTITY)
            turn_b = runtime._desired
            self.assertNotEqual(turn_b.scope_key, scope_a_key)
            self.assertIsNone(runtime._retired_scope_key)
            await _wait_for(lambda: runtime.status()["ready"] and runtime._active_scope_key == turn_b.scope_key)

            self.assertEqual(len(backends), 2)
            self.assertEqual(len(clients), 2)
            backend_b = backends[1]
            session_b = clients[1].session
            self.assertTrue(backend_b.started)
            self.assertIsNotNone(session_b)
            self.assertFalse(session_b.closed)
            ready_status = runtime.status()
            self.assertTrue(ready_status["ready"])
            self.assertTrue(ready_status["capturing"])
            self.assertEqual(ready_status["unavailableReason"], "")
            self.assertIsNone(ready_status["lastFailureCode"])
            self.assertEqual(runtime._route_diagnostic_scope_key, turn_b.scope_key)
            self.assertIsNone(ready_status["routeDiagnostics"]["firstFailure"])
            self.assertEqual(ready_status["routeDiagnostics"]["failureCount"], 0)

            diagnostics_before_late_a = runtime._route_diagnostics_snapshot()
            status_before_late_a = runtime.status()
            self.assertFalse(backends[0].capture_fence())

            async def late_a_events():
                yield {
                    "type": "event", "platform": "qq", "source_id": "main",
                    "event": "speech_start", "turn_id": "late-A-turn",
                }

            session_a.events = late_a_events
            await runtime._events_worker(turn_a, session_a)
            self.assertTrue(callable(backends[0].on_failure))
            backends[0].on_failure("capture_processing_failed")
            runtime._revoke_route_sync("audio_scope_stale", desired=turn_a)

            self.assertEqual(runtime._route_diagnostics_snapshot(), diagnostics_before_late_a)
            status_after_late_a = runtime.status()
            self.assertEqual(status_after_late_a, status_before_late_a)
            for field in (
                "ready", "capturing", "unavailableReason", "lastFailureCode",
                "activeTurn", "routeDiagnostics",
            ):
                self.assertEqual(status_after_late_a[field], status_before_late_a[field], field)
            self.assertEqual(runtime._active_scope_key, turn_b.scope_key)
            self.assertTrue(session_b is runtime._session)
            self.assertFalse(session_b.closed)
        finally:
            await runtime.stop()

    async def test_startup_retirement_after_session_creation_uses_supervisor_cleanup(self) -> None:
        backend = _FakeQQPCMDevice()
        client = _StartupClient()
        route_calls = 0

        def verify_route(_scope, _config):
            nonlocal route_calls
            route_calls += 1
            if route_calls == 1:
                return QQCallRouteProof(time.monotonic())
            return QQCallRouteRetired()

        runtime = self._new_startup_runtime(
            route_verifier=verify_route,
            backend=backend,
            client_factory=lambda _api: client,
        )
        runtime.update_call(_CALL, _IDENTITY)
        desired = runtime._desired
        runtime.start_supervisor()
        try:
            await asyncio.wait_for(client.create_started.wait(), timeout=2.0)
            await _wait_for(lambda: client.closed and backend.stopped)
            self.assertEqual(route_calls, 2)
            self.assertTrue(backend.cancelled >= 1)
            self.assertFalse(backend.started)
            self.assertIsNotNone(client.session)
            self.assertTrue(client.session.closed)
            self.assertEqual(client.close_effects, 1)
            self.assertEqual(runtime._blocked_scope_key, desired.scope_key)
            self.assertEqual(runtime._retired_scope_key, desired.scope_key)
            self.assertIsNone(runtime._desired)
            status = runtime.status()
            self.assertFalse(status["ready"])
            self.assertFalse(status["capturing"])
            self.assertEqual(status["unavailableReason"], "call_not_connected")
            self.assertIsNone(status["lastFailureCode"])
            self.assertIsNone(status["routeDiagnostics"]["firstFailure"])
            self.assertEqual(status["routeDiagnostics"]["failureCount"], 0)
        finally:
            await runtime.stop()

    async def test_delayed_retirement_response_after_new_scope_is_obsolete(self) -> None:
        verifier_started = asyncio.Event()
        release_verifier = asyncio.Event()

        async def verify_route(_scope, _config):
            verifier_started.set()
            await release_verifier.wait()
            return QQCallRouteRetired()

        runtime = self._new_startup_runtime(route_verifier=verify_route)
        runtime.update_call(_CALL, _IDENTITY)
        turn_a = runtime._desired
        pending_check = asyncio.create_task(runtime._verify_route(turn_a))
        try:
            await asyncio.wait_for(verifier_started.wait(), timeout=1.0)
            call_b = {**_CALL, "scope": {**_SCOPE, "callGeneration": _SCOPE["callGeneration"] + 1}}
            runtime.update_call(call_b, _IDENTITY)
            turn_b = runtime._desired
            diagnostics_after_b = runtime._route_diagnostics_snapshot()
            release_verifier.set()
            with self.assertRaises(_StartupScopeObsolete):
                await pending_check
            self.assertEqual(runtime._desired.scope_key, turn_b.scope_key)
            self.assertIsNone(runtime._retired_scope_key)
            self.assertIsNone(runtime._blocked_scope_key)
            self.assertEqual(runtime._route_diagnostics_snapshot(), diagnostics_after_b)
            self.assertFalse(runtime._route_failed)
            self.assertFalse(runtime._ready)
            self.assertFalse(runtime._capture_enabled)
        finally:
            release_verifier.set()
            if not pending_check.done():
                await asyncio.gather(pending_check, return_exceptions=True)
            await runtime.stop()

    async def test_retirement_commit_is_atomic_with_cross_thread_new_scope_transition(self) -> None:
        checked_for_retire = threading.Event()
        release_retire = threading.Event()
        worker_finished = threading.Event()
        worker_errors: list[BaseException] = []
        transition_errors: list[BaseException] = []
        transition_finished = threading.Event()
        runtime = self._new_startup_runtime(
            route_verifier=lambda _scope, _config: QQCallRouteRetired(),
        )
        runtime.update_call(_CALL, _IDENTITY)
        turn_a = runtime._desired
        original_check = runtime._route_check_current
        check_count = [0]

        def pause_before_retire_commit(desired) -> bool:
            current = original_check(desired)
            if current:
                check_count[0] += 1
                if check_count[0] == 2:
                    checked_for_retire.set()
                    if not release_retire.wait(timeout=2.0):
                        raise AssertionError("timed out waiting to release retirement commit")
            return current

        runtime._route_check_current = pause_before_retire_commit

        def run_retirement_check() -> None:
            async def run() -> None:
                try:
                    await runtime._verify_route(turn_a)
                except _StartupScopeObsolete:
                    return
                raise AssertionError("obsolete retirement response unexpectedly committed")

            try:
                asyncio.run(run())
            except BaseException as exc:
                worker_errors.append(exc)
            finally:
                worker_finished.set()

        def accept_scope_b() -> None:
            try:
                call_b = {
                    **_CALL,
                    "scope": {**_SCOPE, "callGeneration": _SCOPE["callGeneration"] + 1},
                }
                runtime.update_call(call_b, _IDENTITY)
            except BaseException as exc:
                transition_errors.append(exc)
            finally:
                transition_finished.set()

        worker = threading.Thread(target=run_retirement_check, daemon=True)
        worker.start()
        try:
            self.assertTrue(
                await asyncio.to_thread(checked_for_retire.wait, 1.0),
                "retirement check did not reach the commit boundary",
            )
            transition = threading.Thread(target=accept_scope_b, daemon=True)
            transition.start()
            self.assertTrue(
                await asyncio.to_thread(transition_finished.wait, 1.0),
                "cross-thread scope transition did not finish",
            )
            transition.join(timeout=1.0)
            self.assertFalse(transition.is_alive())
            self.assertEqual(transition_errors, [])
            turn_b = runtime._desired
            diagnostics_after_b = runtime._route_diagnostics_snapshot()
            release_retire.set()
            self.assertTrue(await asyncio.to_thread(worker_finished.wait, 1.0))
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(worker_errors, [])
            self.assertEqual(runtime._desired.scope_key, turn_b.scope_key)
            self.assertNotEqual(turn_b.scope_key, turn_a.scope_key)
            self.assertIsNone(runtime._retired_scope_key)
            self.assertIsNone(runtime._blocked_scope_key)
            self.assertEqual(runtime._route_diagnostics_snapshot(), diagnostics_after_b)
            self.assertEqual(runtime._unavailable_reason, "audio_route_pending")
            self.assertIsNone(runtime._failure_code)
            self.assertFalse(runtime._route_failed)
            self.assertFalse(runtime._ready)
            self.assertFalse(runtime._capture_enabled)
        finally:
            release_retire.set()
            worker.join(timeout=1.0)
            if worker.is_alive():
                self.fail("retirement check worker did not stop")
            await runtime.stop()

    async def test_current_route_monitor_failure_still_revokes_active_turn(self) -> None:
        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
            route_verifier=lambda _scope, _config: QQCallRouteRejection("host_input_not_active"),
        )
        runtime.update_call(_CALL, _IDENTITY)
        desired = runtime._desired
        runtime._active_scope_key = desired.scope_key
        runtime._ready = True
        runtime._capture_enabled = True
        runtime._route_verified_at = time.monotonic()

        async def no_wait(_delay):
            return None

        with patch("qqcall_client.qqcall_universal_api.asyncio.sleep", no_wait):
            await runtime._route_monitor(desired)

        self.assertTrue(runtime._route_failed)
        self.assertFalse(runtime._ready)
        self.assertFalse(runtime._capture_enabled)
        self.assertEqual(runtime._failure_code, "universal_api_route_unverified")
        self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailure"], "host_input_not_active")
        await runtime.stop()

    async def test_route_helper_rejection_keeps_public_failure_and_first_reason(self) -> None:
        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
            route_verifier=lambda _scope, _config: QQCallRouteRejection("host_input_not_active"),
        )
        runtime.update_call(_CALL, _IDENTITY)
        desired = runtime._desired
        runtime._active_scope_key = desired.scope_key
        runtime._ready = True
        runtime._capture_enabled = True
        runtime._route_verified_at = time.monotonic()

        async def no_wait(_delay):
            return None

        with patch("qqcall_client.qqcall_universal_api.asyncio.sleep", no_wait):
            await runtime._route_monitor(desired)

        status = runtime.status()
        self.assertFalse(status["ready"])
        self.assertFalse(status["capturing"])
        self.assertEqual(status["unavailableReason"], "universal_api_route_unverified")
        self.assertEqual(status["lastFailureCode"], "universal_api_route_unverified")
        self.assertEqual(status["routeDiagnostics"]["firstFailure"], "host_input_not_active")

        runtime._route_verifier = lambda _scope, _config: QQCallRouteProof(time.monotonic())
        self.assertGreater(await runtime._verify_route(desired), 0)
        self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailure"], "host_input_not_active")

        # A blocked call's latched first failure belongs to its scope, even if
        # adapter identity or peer metadata is refreshed for that same scope.
        session = object()
        runtime._session = session
        blocked_scope_key = desired.scope_key
        refreshed_identity = {**_IDENTITY, "identity": (1, 2, "99998"), "account_id": "99998"}
        runtime.update_call(_CALL, refreshed_identity)
        self.assertEqual(runtime._blocked_scope_key, blocked_scope_key)
        self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailure"], "host_input_not_active")
        self.assertFalse(runtime._capture_transport_fence(desired, session))

        changed_peer = {**_CALL, "peer_user_id": "54321"}
        runtime.update_call(changed_peer, refreshed_identity)
        self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailure"], "host_input_not_active")

        # None is a temporary absence, not a boundary between diagnostic calls.
        runtime.update_call(None, None)
        runtime.update_call(_CALL, refreshed_identity)
        self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailure"], "host_input_not_active")

        replacement = {**_CALL, "scope": {**_SCOPE, "callGeneration": 4}}
        runtime.update_call(replacement, refreshed_identity)
        diagnostics = runtime.status()["routeDiagnostics"]
        self.assertIsNone(diagnostics["firstFailure"])
        self.assertEqual(diagnostics["failureCount"], 0)
        self.assertNotIn("route_diagnostic_scope_key", diagnostics)
        self.assertNotIn("route_diagnostic_scope_key", repr(runtime.status()))
        self.assertNotIn(_SCOPE["callId"], repr(runtime.status()))

    async def test_startup_waits_for_inactive_endpoints_before_open_and_remembers_ready_scope(self) -> None:
        backend = _FakeQQPCMDevice()
        client = _StartupClient()
        answers = [
            QQCallRouteRejection("host_input_not_active"),
            QQCallRouteRejection("host_output_not_active"),
            QQCallRouteProof(time.monotonic()),
            QQCallRouteRejection("host_input_not_active"),
            QQCallRouteProof(time.monotonic()),
            QQCallRouteRejection("host_input_not_active"),
        ]
        route_calls = 0
        identity_checks = 0
        admission_checks = 0

        def verify_route(_scope, _config):
            nonlocal route_calls
            route_calls += 1
            if route_calls <= 5:
                self.assertFalse(backend.started, "route evidence must be ready before the device opens")
            return answers.pop(0)

        def identity_is_current(_identity):
            nonlocal identity_checks
            identity_checks += 1
            return True

        def admit_peer(_peer):
            nonlocal admission_checks
            admission_checks += 1
            return True

        runtime = self._new_startup_runtime(
            route_verifier=verify_route,
            backend=backend,
            client_factory=lambda _api: client,
            identity_is_current=identity_is_current,
            admit_peer=admit_peer,
        )
        runtime.update_call(_CALL, _IDENTITY)
        desired = runtime._desired
        try:
            with (
                patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 0.8),
                patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_RETRY_DELAY", 0.002),
            ):
                await runtime._start_scope(desired)
            self.assertTrue(runtime.status()["ready"])
            self.assertEqual(route_calls, 5)
            self.assertEqual(client.create_calls, 1)
            self.assertGreaterEqual(identity_checks, 15)
            self.assertGreaterEqual(admission_checks, 10)
            self.assertTrue(backend.started)
            self.assertIsNone(runtime.status()["routeDiagnostics"]["firstFailure"])

            await runtime._stop_resources(interrupt=True)
            runtime.update_call(None, None)
            runtime.update_call(_CALL, _IDENTITY)
            self.assertEqual(runtime._ever_ready_scope_key, desired.scope_key)
            with self.assertRaises(QQCallVoiceError) as rejected:
                await runtime._verify_route(
                    desired,
                    startup_pending=True,
                    deadline=time.monotonic() + 0.2,
                )
            self.assertEqual(rejected.exception.code, "universal_api_route_unverified")
            self.assertEqual(route_calls, 6, "an already-ready scope must not get startup retries")
            self.assertEqual(
                runtime.status()["routeDiagnostics"]["firstFailure"],
                "host_input_not_active",
            )
        finally:
            await runtime.stop()

    async def test_startup_inactive_exhaustion_latches_only_last_reason_and_caps_sleep_to_deadline(self) -> None:
        backend = _FakeQQPCMDevice()
        route_calls: list[float] = []
        sleep_observations: list[tuple[float, float]] = []
        shared_deadlines: list[float] = []
        runtime = self._new_startup_runtime(
            route_verifier=lambda _scope, _config: (
                route_calls.append(clock.monotonic())
                or QQCallRouteRejection("host_output_not_active")
            ),
            backend=backend,
        )
        runtime.update_call(_CALL, _IDENTITY)
        desired = runtime._desired
        timeout = 0.055
        real_sleep = asyncio.sleep

        class _DeterministicMonotonic:
            def __init__(self) -> None:
                self.now = 100.0

            def monotonic(self) -> float:
                return self.now

            @staticmethod
            def time() -> float:
                return time.time()

        clock = _DeterministicMonotonic()
        original_startup_verify = runtime._verify_startup_route

        async def capture_shared_deadline(turn, *, deadline):
            shared_deadlines.append(deadline)
            return await original_startup_verify(turn, deadline=deadline)

        runtime._verify_startup_route = capture_shared_deadline

        async def bounded_sleep(delay):
            # The fake clock cannot be preempted between production's budget
            # calculation and this call, so this checks the selected final
            # sleep against the actual deadline passed through _start_scope.
            deadline = shared_deadlines[-1]
            remaining = deadline - clock.monotonic()
            sleep_observations.append((delay, remaining))
            self.assertEqual(delay, min(0.02, remaining))
            clock.now += delay
            await real_sleep(0)

        try:
            with (
                patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", timeout),
                patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_RETRY_DELAY", 0.02),
                patch("qqcall_client.qqcall_universal_api.asyncio.sleep", bounded_sleep),
                patch("qqcall_client.qqcall_universal_api.time", clock),
            ):
                with self.assertRaises(QQCallVoiceError) as rejected:
                    await runtime._start_scope(desired)
            self.assertEqual(rejected.exception.code, "universal_api_route_unverified")
            self.assertEqual(len(shared_deadlines), 1)
            deadline = shared_deadlines[0]
            self.assertAlmostEqual(deadline - 100.0, timeout)
            self.assertGreaterEqual(len(route_calls), 2)
            self.assertTrue(all(value < deadline for value in route_calls))
            self.assertTrue(sleep_observations)
            self.assertLess(sleep_observations[-1][0], 0.02)
            self.assertEqual(sleep_observations[-1][0], sleep_observations[-1][1])
            self.assertEqual(clock.monotonic(), deadline)
            self.assertFalse(backend.started)
            self.assertIsNone(runtime._client)
            diagnostics = runtime.status()["routeDiagnostics"]
            self.assertEqual(diagnostics["firstFailure"], "host_output_not_active")
            self.assertEqual(diagnostics["failureCount"], 1)
        finally:
            await runtime.stop()

    async def test_startup_noneligible_identity_and_acl_failures_do_not_retry_inactive_result(self) -> None:
        async def run_case(kind):
            backend = _FakeQQPCMDevice()
            route_calls = 0
            identity_calls = 0
            admission_calls = 0

            def verify_route(_scope, _config):
                nonlocal route_calls
                route_calls += 1
                reason = "endpoint_not_found" if kind == "noneligible" else "host_input_not_active"
                return QQCallRouteRejection(reason)

            def identity_is_current(_identity):
                nonlocal identity_calls
                identity_calls += 1
                return not (kind == "identity" and identity_calls == 4)

            def admit_peer(_peer):
                nonlocal admission_calls
                admission_calls += 1
                return not (kind == "acl" and admission_calls == 3)

            client = _StartupClient()
            runtime = self._new_startup_runtime(
                route_verifier=verify_route,
                backend=backend,
                client_factory=lambda _api: client,
                identity_is_current=identity_is_current,
                admit_peer=admit_peer,
            )
            runtime.update_call(_CALL, _IDENTITY)
            try:
                with (
                    patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 0.5),
                    patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_RETRY_DELAY", 0.002),
                ):
                    with self.assertRaises(QQCallVoiceError) as rejected:
                        await runtime._start_scope(runtime._desired)
                expected = {
                    "noneligible": "universal_api_route_unverified",
                    "identity": "audio_scope_stale",
                    "acl": "peer_not_admitted",
                }[kind]
                self.assertEqual(rejected.exception.code, expected)
                self.assertEqual(route_calls, 1)
                self.assertEqual(client.create_calls, 0)
                self.assertFalse(backend.started)
                failure = runtime.status()["routeDiagnostics"]["firstFailure"]
                if kind == "noneligible":
                    self.assertEqual(failure, "endpoint_not_found")
                elif kind == "identity":
                    self.assertEqual(failure, "identity_check_false")
                else:
                    self.assertIsNone(failure)
            finally:
                await runtime.stop()

        for kind in ("noneligible", "identity", "acl"):
            with self.subTest(kind=kind):
                await run_case(kind)

    async def test_startup_route_and_uplink_create_share_one_deadline_with_late_cleanup(self) -> None:
        backend = _FakeQQPCMDevice()
        release_create = asyncio.Event()
        client = _StartupClient(create_gate=release_create)
        runtime = self._new_startup_runtime(
            route_verifier=lambda _scope, _config: QQCallRouteProof(time.monotonic()),
            backend=backend,
            client_factory=lambda _api: client,
        )
        runtime.update_call(_CALL, _IDENTITY)
        timeout = 0.06
        started_at = time.monotonic()
        try:
            with patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", timeout):
                with self.assertRaises(QQCallVoiceError) as rejected:
                    await runtime._start_scope(runtime._desired)
            elapsed = time.monotonic() - started_at
            self.assertEqual(rejected.exception.code, "universal_api_route_unverified")
            self.assertLess(elapsed, 0.25)
            self.assertTrue(client.create_started.is_set())
            self.assertFalse(backend.started)
            self.assertFalse(client.closed)
            await runtime._stop_resources(interrupt=True)
            release_create.set()
            await _wait_for(lambda: client.closed and client.session is not None and client.session.closed)
        finally:
            release_create.set()
            await runtime.stop()

    async def test_startup_late_route_proof_after_shared_deadline_never_opens_backend(self) -> None:
        backend = _FakeQQPCMDevice()
        verifier_started = asyncio.Event()

        async def verify_route(_scope, _config):
            verifier_started.set()
            await asyncio.sleep(0.08)
            return QQCallRouteProof(time.monotonic())

        runtime = self._new_startup_runtime(route_verifier=verify_route, backend=backend)
        runtime.update_call(_CALL, _IDENTITY)
        try:
            with patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 0.025):
                with self.assertRaises(QQCallVoiceError) as rejected:
                    await runtime._start_scope(runtime._desired)
            self.assertTrue(verifier_started.is_set())
            self.assertEqual(rejected.exception.code, "universal_api_route_unverified")
            self.assertFalse(backend.started)
            self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailure"], "route_check_timeout")
        finally:
            await runtime.stop()

    async def test_startup_discards_late_route_rejection_after_terminal_transition(self) -> None:
        verifier_entered = asyncio.Event()
        release_verifier = asyncio.Event()

        async def delayed_rejection(_scope, _config):
            verifier_entered.set()
            await release_verifier.wait()
            return QQCallRouteRejection("host_input_not_active")

        runtime = self._new_startup_runtime(
            route_verifier=delayed_rejection,
            backend=_FakeQQPCMDevice(),
        )
        runtime.update_call(_CALL, _IDENTITY)
        desired = runtime._desired
        startup = asyncio.create_task(
            runtime._verify_startup_route(desired, deadline=time.monotonic() + 1.0),
        )
        try:
            await asyncio.wait_for(verifier_entered.wait(), timeout=0.5)
            runtime.update_call(None, None)
            release_verifier.set()
            with self.assertRaises(_StartupScopeObsolete):
                await asyncio.wait_for(startup, timeout=0.5)
            diagnostics = runtime.status()["routeDiagnostics"]
            self.assertIsNone(diagnostics["firstFailure"])
            self.assertEqual(diagnostics["failureCount"], 0)
        finally:
            release_verifier.set()
            if not startup.done():
                startup.cancel()
                await asyncio.gather(startup, return_exceptions=True)
            await runtime.stop()

    async def test_scope_change_cancels_pending_retry_sleep_before_replacement_start(self) -> None:
        first = {**_CALL, "scope": dict(_SCOPE)}
        second_scope = {**_SCOPE, "callGeneration": 4}
        second = {**_CALL, "scope": second_scope}
        backends: list[_FakeQQPCMDevice] = []
        clients = [_StartupClient(), _StartupClient()]
        first_route_calls = 0
        retry_sleep_started = asyncio.Event()
        original_sleep = asyncio.sleep

        def backend_factory(_turn):
            backend = _FakeQQPCMDevice()
            backends.append(backend)
            return backend

        def route_verifier(scope, _config):
            nonlocal first_route_calls
            if scope["callGeneration"] == _SCOPE["callGeneration"]:
                first_route_calls += 1
                return QQCallRouteRejection("host_input_not_active")
            return QQCallRouteProof(time.monotonic())

        runtime = self._new_startup_runtime(
            route_verifier=route_verifier,
            backend_factory=backend_factory,
            client_factory=lambda _api: clients.pop(0),
        )

        async def tracked_sleep(delay):
            if abs(delay - 0.2) < 0.001:
                retry_sleep_started.set()
            await original_sleep(delay)

        runtime.update_call(first, _IDENTITY)
        runtime.start_supervisor()
        try:
            with (
                patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 2.0),
                patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_RETRY_DELAY", 0.2),
                patch("qqcall_client.qqcall_universal_api.asyncio.sleep", tracked_sleep),
            ):
                await asyncio.wait_for(retry_sleep_started.wait(), timeout=1.0)
                runtime.update_call(second, _IDENTITY)
                await _wait_for(lambda: runtime.status()["ready"] and runtime._active_scope_key == runtime._desired.scope_key)
            self.assertEqual(first_route_calls, 1)
            self.assertEqual(len(backends), 2)
            self.assertTrue(backends[0].stopped)
            self.assertTrue(backends[1].started)
            self.assertIsNone(runtime._blocked_scope_key)
            self.assertIsNone(runtime.status()["routeDiagnostics"]["firstFailure"])
        finally:
            await runtime.stop()

    async def test_scope_change_cancels_pending_identity_await_without_blocking_replacement(self) -> None:
        second_scope = {**_SCOPE, "callGeneration": 5}
        second = {**_CALL, "scope": second_scope}
        identity_entered = asyncio.Event()
        identity_cancelled = asyncio.Event()
        identity_calls = 0
        backends: list[_FakeQQPCMDevice] = []
        client = _StartupClient()

        async def identity_is_current(_identity):
            nonlocal identity_calls
            identity_calls += 1
            if identity_calls == 1:
                identity_entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    identity_cancelled.set()
                    raise
            return True

        async def verify_route(scope, _config):
            return QQCallRouteProof(time.monotonic())

        runtime = self._new_startup_runtime(
            route_verifier=verify_route,
            backend_factory=lambda _turn: backends.append(_FakeQQPCMDevice()) or backends[-1],
            client_factory=lambda _api: client,
            identity_is_current=identity_is_current,
        )
        runtime.update_call(_CALL, _IDENTITY)
        runtime.start_supervisor()
        try:
            with patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 2.0):
                await asyncio.wait_for(identity_entered.wait(), timeout=1.0)
                replacement = {**_CALL, "scope": second_scope}
                runtime.update_call(replacement, _IDENTITY)
                await _wait_for(lambda: runtime.status()["ready"])
            self.assertTrue(identity_cancelled.is_set())
            self.assertEqual(len(backends), 1, "the obsolete identity wait must not create its backend")
            self.assertTrue(backends[0].started)
            self.assertIsNone(runtime._blocked_scope_key)
            self.assertIsNone(runtime.status()["routeDiagnostics"]["firstFailure"])
        finally:
            await runtime.stop()

    async def test_scope_change_cancels_pending_route_verifier_without_blocking_replacement(self) -> None:
        second_scope = {**_SCOPE, "callGeneration": 7}
        second = {**_CALL, "scope": second_scope}
        verifier_entered = asyncio.Event()
        verifier_cancelled = asyncio.Event()
        backends: list[_FakeQQPCMDevice] = []

        async def verify_route(scope, _config):
            if scope["callGeneration"] == _SCOPE["callGeneration"]:
                verifier_entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    verifier_cancelled.set()
                    raise
            return QQCallRouteProof(time.monotonic())

        runtime = self._new_startup_runtime(
            route_verifier=verify_route,
            backend_factory=lambda _turn: backends.append(_FakeQQPCMDevice()) or backends[-1],
            client_factory=lambda _api: _StartupClient(),
        )
        runtime.update_call(_CALL, _IDENTITY)
        runtime.start_supervisor()
        try:
            with patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 2.0):
                await asyncio.wait_for(verifier_entered.wait(), timeout=1.0)
                runtime.update_call(second, _IDENTITY)
                await _wait_for(lambda: runtime.status()["ready"])
            self.assertTrue(verifier_cancelled.is_set())
            self.assertEqual(len(backends), 2)
            self.assertTrue(backends[0].stopped)
            self.assertFalse(backends[0].started)
            self.assertTrue(backends[1].started)
            self.assertIsNone(runtime._blocked_scope_key)
            self.assertIsNone(runtime.status()["routeDiagnostics"]["firstFailure"])
        finally:
            await runtime.stop()

    async def test_stop_cancels_pending_route_verifier_before_startup_resources_open(self) -> None:
        verifier_entered = asyncio.Event()
        verifier_cancelled = asyncio.Event()
        backend = _FakeQQPCMDevice()

        async def verify_route(_scope, _config):
            verifier_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                verifier_cancelled.set()
                raise

        runtime = self._new_startup_runtime(
            route_verifier=verify_route,
            backend=backend,
            client_factory=lambda _api: _StartupClient(),
        )
        runtime.update_call(_CALL, _IDENTITY)
        runtime.start_supervisor()
        with patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 2.0):
            await asyncio.wait_for(verifier_entered.wait(), timeout=1.0)
            await runtime.stop()
        self.assertTrue(verifier_cancelled.is_set())
        self.assertFalse(backend.started)
        self.assertTrue(backend.stopped)
        self.assertFalse(runtime.status()["ready"])

    async def test_scope_change_during_uplink_create_cleans_late_session_and_starts_replacement(self) -> None:
        second_scope = {**_SCOPE, "callGeneration": 6}
        second = {**_CALL, "scope": second_scope}
        release_first_create = asyncio.Event()
        first_client = _StartupClient(create_gate=release_first_create)
        second_client = _StartupClient()
        clients = [first_client, second_client]
        backends: list[_FakeQQPCMDevice] = []

        def make_backend(_turn):
            backend = _FakeQQPCMDevice()
            backends.append(backend)
            return backend

        runtime = self._new_startup_runtime(
            route_verifier=lambda _scope, _config: QQCallRouteProof(time.monotonic()),
            backend_factory=make_backend,
            client_factory=lambda _api: clients.pop(0),
        )
        runtime.update_call(_CALL, _IDENTITY)
        runtime.start_supervisor()
        try:
            with patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 2.0):
                await asyncio.wait_for(first_client.create_started.wait(), timeout=1.0)
                runtime.update_call(second, _IDENTITY)
                await _wait_for(lambda: runtime.status()["ready"])
            self.assertEqual(len(backends), 2)
            self.assertFalse(backends[0].started)
            self.assertTrue(backends[0].stopped)
            self.assertTrue(backends[1].started)
            self.assertIsNone(runtime._blocked_scope_key)

            release_first_create.set()
            await _wait_for(lambda: first_client.closed and first_client.session is not None and first_client.session.closed)
            self.assertIsNone(runtime.status()["routeDiagnostics"]["firstFailure"])
        finally:
            release_first_create.set()
            await runtime.stop()

    async def test_scope_replacement_during_postcreate_interrupt_keeps_cleanup_ownership(self) -> None:
        second_scope = {**_SCOPE, "callGeneration": 8}
        second = {**_CALL, "scope": second_scope}
        interrupt_started = asyncio.Event()
        release_interrupt = asyncio.Event()
        old_session = _StartupSession(
            interrupt_gate=release_interrupt,
            interrupt_started=interrupt_started,
        )
        first_client = _StartupClient(session_factory=lambda: old_session)
        second_client = _StartupClient()
        clients = [first_client, second_client]
        backends: list[_FakeQQPCMDevice] = []
        runtime = None

        def admit_peer(_peer):
            desired = runtime._desired if runtime is not None else None
            if (
                desired is not None
                and desired.scope["callGeneration"] == _SCOPE["callGeneration"]
                and first_client.session is not None
            ):
                return False
            return True

        runtime = self._new_startup_runtime(
            route_verifier=lambda _scope, _config: QQCallRouteProof(time.monotonic()),
            backend_factory=lambda _turn: backends.append(_FakeQQPCMDevice()) or backends[-1],
            client_factory=lambda _api: clients.pop(0),
            admit_peer=admit_peer,
        )
        runtime.update_call(_CALL, _IDENTITY)
        runtime.start_supervisor()
        try:
            with patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 2.0):
                await asyncio.wait_for(interrupt_started.wait(), timeout=1.0)
                self.assertFalse(old_session.closed)
                self.assertFalse(first_client.closed)
                runtime.update_call(second, _IDENTITY)
                # Let the supervisor cancel obsolete startup while interrupt
                # remains gated; releasing it then lets owned cleanup finish.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                release_interrupt.set()
                await _wait_for(lambda: runtime.status()["ready"])

            self.assertEqual(runtime._active_scope_key, runtime._desired.scope_key)
            self.assertTrue(old_session.interrupted)
            self.assertTrue(old_session.closed)
            self.assertEqual(old_session.close_effects, 1)
            self.assertTrue(first_client.closed)
            self.assertEqual(first_client.close_effects, 1)
            self.assertEqual(len(backends), 2)
            self.assertTrue(backends[0].stopped)
            self.assertTrue(backends[1].started)
            self.assertIs(second_client.session, runtime._session)
            self.assertFalse(second_client.closed)
            self.assertIsNone(runtime._blocked_scope_key)
        finally:
            release_interrupt.set()
            if not first_client.closed:
                await first_client.close()
            await runtime.stop()

    async def test_scope_replacement_during_precreate_client_close_retains_owner(self) -> None:
        second_scope = {**_SCOPE, "callGeneration": 9}
        second = {**_CALL, "scope": second_scope}
        close_started = asyncio.Event()
        release_close = asyncio.Event()
        first_client = _StartupClient(
            close_gate=release_close,
            close_started=close_started,
        )
        second_client = _StartupClient()
        clients = [first_client, second_client]
        backends: list[_FakeQQPCMDevice] = []
        runtime = None

        def admit_peer(_peer):
            desired = runtime._desired if runtime is not None else None
            return not (
                desired is not None
                and desired.scope["callGeneration"] == _SCOPE["callGeneration"]
                and runtime._client is first_client
            )

        runtime = self._new_startup_runtime(
            route_verifier=lambda _scope, _config: QQCallRouteProof(time.monotonic()),
            backend_factory=lambda _turn: backends.append(_FakeQQPCMDevice()) or backends[-1],
            client_factory=lambda _api: clients.pop(0),
            admit_peer=admit_peer,
        )
        runtime.update_call(_CALL, _IDENTITY)
        runtime.start_supervisor()
        try:
            with patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 2.0):
                await asyncio.wait_for(close_started.wait(), timeout=1.0)
                runtime.update_call(second, _IDENTITY)
                await asyncio.wait_for(first_client.close_cancelled.wait(), timeout=1.0)
                release_close.set()
                await _wait_for(lambda: runtime.status()["ready"])

            self.assertTrue(first_client.closed)
            self.assertEqual(first_client.close_effects, 1)
            self.assertEqual(len(backends), 2)
            self.assertTrue(backends[0].stopped)
            self.assertTrue(backends[1].started)
            self.assertIs(second_client.session, runtime._session)
            self.assertIsNone(runtime._blocked_scope_key)
        finally:
            release_close.set()
            if not first_client.closed:
                await first_client.close()
            await runtime.stop()

    async def test_stop_during_postcreate_session_close_finishes_owned_cleanup_once(self) -> None:
        close_started = asyncio.Event()
        release_close = asyncio.Event()
        old_session = _StartupSession(close_gate=release_close, close_started=close_started)
        client = _StartupClient(session_factory=lambda: old_session)
        backend = _FakeQQPCMDevice()
        runtime = None

        def admit_peer(_peer):
            return not (runtime is not None and runtime._desired is not None and client.session is not None)

        runtime = self._new_startup_runtime(
            route_verifier=lambda _scope, _config: QQCallRouteProof(time.monotonic()),
            backend=backend,
            client_factory=lambda _api: client,
            admit_peer=admit_peer,
        )
        runtime.update_call(_CALL, _IDENTITY)
        runtime.start_supervisor()
        stop_task = None
        try:
            with patch("qqcall_client.qqcall_universal_api._STARTUP_ROUTE_TIMEOUT", 2.0):
                await asyncio.wait_for(close_started.wait(), timeout=1.0)
                self.assertFalse(old_session.closed)
                self.assertFalse(client.closed)
                stop_task = asyncio.create_task(runtime.stop())
                for _ in range(20):
                    await asyncio.sleep(0)
                self.assertFalse(old_session.close_cancelled.is_set())
                self.assertFalse(stop_task.done(), "stop must wait for the transferred owner cleanup")
                release_close.set()
                await asyncio.wait_for(stop_task, timeout=3.0)

            self.assertTrue(old_session.closed)
            self.assertEqual(old_session.close_effects, 1)
            self.assertTrue(client.closed)
            self.assertEqual(client.close_effects, 1)
            self.assertTrue(backend.stopped)
            self.assertFalse(runtime.status()["ready"])
        finally:
            release_close.set()
            if stop_task is not None and not stop_task.done():
                await asyncio.gather(stop_task, return_exceptions=True)
            elif stop_task is None:
                await runtime.stop()

    async def test_route_timeout_and_malformed_proof_have_distinct_fixed_reasons(self) -> None:
        async def slow_verifier(_scope, _config):
            await asyncio.sleep(1)
            return True

        timed = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True, route_verifier=slow_verifier,
        )
        timed.update_call(_CALL, _IDENTITY)
        with patch("qqcall_client.qqcall_universal_api._ROUTE_VERIFIER_TIMEOUT", 0.01):
            with self.assertRaises(QQCallVoiceError) as timeout_error:
                await timed._verify_route(timed._desired)
        self.assertEqual(timeout_error.exception.code, "universal_api_route_unverified")
        timed_diagnostics = timed.status()["routeDiagnostics"]
        self.assertEqual(timed_diagnostics["firstFailure"], "route_check_timeout")
        self.assertEqual(timed_diagnostics["routeCheckStage"], "scope_validation")
        self.assertEqual(timed_diagnostics["firstFailureStage"], "scope_validation")

        malformed = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
            route_verifier=lambda _scope, _config: {"private": "proof-payload-must-not-escape"},
        )
        malformed.update_call(_CALL, _IDENTITY)
        with self.assertRaises(QQCallVoiceError) as malformed_error:
            await malformed._verify_route(malformed._desired)
        diagnostics = malformed.status()["routeDiagnostics"]
        self.assertEqual(malformed_error.exception.code, "universal_api_route_unverified")
        self.assertEqual(diagnostics["firstFailure"], "route_proof_invalid")
        self.assertEqual(diagnostics["routeCheckStage"], "scope_validation")
        self.assertIsNone(diagnostics["firstFailureStage"])
        self.assertNotIn("proof-payload-must-not-escape", repr(diagnostics))

    async def test_route_verifier_accepts_realistic_observer_duration_without_restamping_proof(self) -> None:
        async def verify_route(_scope, _config):
            observed_at = time.monotonic()
            await asyncio.sleep(1.1)
            return QQCallRouteProof(observed_at)

        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True, route_verifier=verify_route,
        )
        runtime.update_call(_CALL, _IDENTITY)
        observed_at = await runtime._verify_route(runtime._desired)
        runtime._route_verified_at = observed_at

        age = time.monotonic() - observed_at
        self.assertGreaterEqual(age, 1.05)
        self.assertLess(age, 2.5)
        self.assertTrue(runtime._route_is_fresh())

    async def test_route_verifier_still_rejects_work_beyond_its_budget(self) -> None:
        async def too_slow(_scope, _config):
            await asyncio.sleep(2.8)
            return QQCallRouteProof(time.monotonic())

        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True, route_verifier=too_slow,
        )
        runtime.update_call(_CALL, _IDENTITY)
        with self.assertRaises(QQCallVoiceError) as raised:
            await asyncio.wait_for(runtime._verify_route(runtime._desired), timeout=3.0)
        self.assertEqual(raised.exception.code, "universal_api_route_unverified")
        diagnostics = runtime.status()["routeDiagnostics"]
        self.assertEqual(diagnostics["firstFailure"], "route_check_timeout")
        self.assertEqual(diagnostics["firstFailureStage"], "scope_validation")

    async def test_route_stage_recorders_are_exact_scope_bound_and_latch_only_timeout_stage(self) -> None:
        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
        )
        unbound_writer = runtime.make_route_stage_recorder(_SCOPE)
        unbound_writer("pre_context")
        self.assertIsNone(runtime.status()["routeDiagnostics"]["routeCheckStage"])
        runtime.update_call(_CALL, _IDENTITY)
        first_scope = runtime._desired.scope
        first_key = runtime._desired.scope_key
        first_writer = runtime.make_route_stage_recorder(first_scope)
        first_writer("pre_context")
        wrong_scope_writer = runtime.make_route_stage_recorder({**first_scope, "callGeneration": 99})
        wrong_scope_writer("helper_request")

        class _StageSubclass(str):
            pass

        for invalid in (_StageSubclass("helper_request"), object(), "private-stage-value"):
            first_writer(invalid)
        self.assertEqual(runtime.status()["routeDiagnostics"]["routeCheckStage"], "pre_context")

        second_call = {**_CALL, "scope": {**_SCOPE, "callGeneration": 4}}
        runtime.update_call(second_call, _IDENTITY)
        second_scope = runtime._desired.scope
        second_key = runtime._desired.scope_key
        second_writer = runtime.make_route_stage_recorder(second_scope)
        second_writer("helper_request")
        first_writer("post_context")
        diagnostics = runtime.status()["routeDiagnostics"]
        self.assertEqual(diagnostics["routeCheckStage"], "helper_request")
        self.assertIsNone(diagnostics["firstFailure"])

        runtime._record_route_diagnostic_failure("route_check_timeout", second_key)
        first_writer("proof_validation")
        runtime._record_route_diagnostic_failure("route_proof_stale", second_key)
        diagnostics = runtime.status()["routeDiagnostics"]
        self.assertEqual(diagnostics["firstFailure"], "route_check_timeout")
        self.assertEqual(diagnostics["firstFailureStage"], "helper_request")
        self.assertEqual(diagnostics["routeCheckStage"], "helper_request")

        refreshed_identity = {**_IDENTITY, "identity": (3, 4, "99999")}
        runtime.update_call(second_call, refreshed_identity)
        self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailureStage"], "helper_request")
        runtime.update_call(None, None)
        runtime.update_call(second_call, refreshed_identity)
        self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailureStage"], "helper_request")

        replacement = {**_CALL, "scope": {**_SCOPE, "callGeneration": 5}}
        runtime.update_call(replacement, refreshed_identity)
        diagnostics = runtime.status()["routeDiagnostics"]
        self.assertIsNone(diagnostics["routeCheckStage"])
        self.assertIsNone(diagnostics["firstFailureStage"])
        self.assertIsNone(diagnostics["firstFailure"])
        self.assertNotEqual(first_key, second_key)

    async def test_terminal_call_transition_does_not_reopen_capture_fence_or_replace_first_failure(self) -> None:
        runtime = QQCallVoiceRuntime(
            platform="qq", send_message=lambda _message: None, admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
        )
        runtime.update_call(_CALL, _IDENTITY)
        desired = runtime._desired
        session = object()
        runtime._session = session
        runtime._active_scope_key = desired.scope_key
        runtime._ready = True
        runtime._capture_enabled = True
        runtime._route_verified_at = time.monotonic() - 3.0

        self.assertFalse(runtime._capture_transport_fence(desired, session))
        self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailure"], "route_proof_stale")
        runtime._revoke_route_sync("audio_scope_stale")
        runtime.update_call(None, None)

        self.assertFalse(runtime._capture_transport_fence(desired, session))
        self.assertEqual(runtime.status()["routeDiagnostics"]["firstFailure"], "route_proof_stale")
        self.assertFalse(runtime.status()["capturing"])


if __name__ == "__main__":
    unittest.main()
