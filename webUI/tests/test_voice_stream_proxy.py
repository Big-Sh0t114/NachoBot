from __future__ import annotations

import base64
import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI


WEBUI_DIR = Path(__file__).resolve().parents[1]


def _load_voice_routes():
    """Load the route module with inert dependencies so tests never touch webUI/data."""

    voice_calls = types.ModuleType("voice_calls")

    class VoiceCallError(RuntimeError):
        def __init__(self, message: str, status_code: int = 400):
            super().__init__(message)
            self.status_code = status_code

    class VoiceCallStore:
        pass

    voice_calls.CALL_LEASE_SECONDS = 30
    voice_calls.MAX_MESSAGE_CHARS = 10_000
    voice_calls.VoiceCallError = VoiceCallError
    voice_calls.VoiceCallStore = VoiceCallStore
    voice_calls.canonical_core_user_id = lambda conversation_id: f"webui_{conversation_id}"
    voice_calls.validate_conversation_id = lambda conversation_id: conversation_id

    tts_manager = types.ModuleType("tts_manager")

    class TTSManager:
        pass

    class TTSGenerationError(RuntimeError):
        pass

    class TTSUnavailableError(RuntimeError):
        pass

    tts_manager.TTSManager = TTSManager
    tts_manager.TTSGenerationError = TTSGenerationError
    tts_manager.TTSUnavailableError = TTSUnavailableError
    tts_manager._get_core_auth_token = lambda: "unit-test-core-token"
    tts_manager._get_core_base_url = lambda: "http://core.invalid"

    chat_backend = types.ModuleType("chat_backend")

    class ChatBackendError(RuntimeError):
        def __init__(self, message: str, status_code: int = 502):
            super().__init__(message)
            self.status_code = status_code

    chat_backend.ChatBackendError = ChatBackendError

    module_name = "_webui_voice_routes_proxy_test"
    spec = importlib.util.spec_from_file_location(module_name, WEBUI_DIR / "voice_routes.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load voice_routes.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "voice_calls": voice_calls,
            "tts_manager": tts_manager,
            "chat_backend": chat_backend,
            module_name: module,
        },
    ):
        spec.loader.exec_module(module)
    return module


voice_routes = _load_voice_routes()


class _FakeStore:
    def __init__(self):
        self.calls = {
            "call-a": {"id": "call-a", "status": "active", "generation": 7},
        }

    def get_call(self, call_id: str):
        call = self.calls.get(call_id)
        return dict(call) if call is not None else None

    def message_for_tts(self, call_id: str, message_id: str, generation: int):
        call = self.get_call(call_id)
        if call is None or call["status"] != "active" or call["generation"] != generation:
            raise voice_routes.VoiceCallError("stale", 409)
        return {"message": {"content": "hello", "id": message_id}}

    def interrupt(self, call_id: str, generation: int) -> int:
        call = self.calls.get(call_id)
        if call is None or call["status"] != "active" or call["generation"] != generation:
            raise voice_routes.VoiceCallError("stale", 409)
        call["generation"] += 1
        return call["generation"]

    def end_call(self, call_id: str):
        call = self.calls.get(call_id)
        if call is None:
            raise voice_routes.VoiceCallError("missing", 404)
        call["status"] = "ended"
        return dict(call)


class _FakeTTSManager:
    def __init__(self):
        self.health = {
            "status": "ok",
            "desired_profile": "full",
            "capabilities": {"perception": ["audio.transcribe.v1"]},
        }

    async def status(self, strict: bool = True):
        return {"ready": True}

    def _request_json(self, *_args):
        return self.health


class _CoreMock:
    def __init__(self):
        self.requests: list[tuple[str, dict, str | None]] = []
        self.stream_serial = 0
        self.tts_stream_status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        path = request.url.path
        self.requests.append((path, payload, request.headers.get("authorization")))
        if path.endswith("/tts/stream"):
            if self.tts_stream_status != 200:
                return httpx.Response(self.tts_stream_status, json={"detail": "unsupported"})
            return httpx.Response(
                200,
                content=b"\x01\x00\x02\x00",
                headers={
                    "X-TTS-Stream-Version": "1",
                    "X-Audio-Sample-Rate": "24000",
                    "X-Audio-Channels": "1",
                    "X-Audio-Sample-Width": "2",
                    "X-Audio-Codec": "pcm_s16le",
                },
            )
        if path.endswith("/start"):
            self.stream_serial += 1
            return httpx.Response(200, json={"stream_id": f"core-stream-{self.stream_serial}"})
        if path.endswith("/chunk"):
            return httpx.Response(200, json={"seq": payload["seq"], "partial_text": "partial"})
        if path.endswith("/finish"):
            return httpx.Response(200, json={"text": "recognized words", "result_id": "receipt-secret"})
        if path.endswith("/abort"):
            return httpx.Response(200, json={"aborted": True})
        return httpx.Response(404, json={"detail": "unknown"})


class VoiceStreamProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = _FakeStore()
        self.tts = _FakeTTSManager()
        self.core = _CoreMock()

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(self.core.handle), **kwargs)

        self.proxy = voice_routes.CoreAudioStreamProxy(client_factory=client_factory)
        self.router = voice_routes.create_voice_router(
            self.store,
            chat_backend=object(),
            tts_mgr=self.tts,
            audio_stream_proxy=self.proxy,
        )
        self.app = FastAPI()
        self.app.include_router(self.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://webui.test",
        )
        await self.proxy.start()

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.proxy.aclose()

    def path(self, operation: str) -> str:
        return f"/api/chat/calls/call-a/audio/stream/{operation}"

    async def test_tts_stream_interface_proxies_core_pcm(self):
        real_client = httpx.AsyncClient

        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(self.core.handle), **kwargs)

        with patch.object(voice_routes.httpx, "AsyncClient", side_effect=client_factory):
            response = await self.client.post(
                "/api/chat/calls/call-a/tts/stream",
                json={"message_id": "reply-1", "generation": 7},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"\x01\x00\x02\x00")
        self.assertEqual(response.headers["x-audio-codec"], "pcm_s16le")
        self.assertIn(
            ("/api/multimodal/tts/stream", {"text": "hello", "platform": "webui"}, "Bearer unit-test-core-token"),
            self.core.requests,
        )

    async def test_tts_stream_unavailable_exposes_buffered_fallback_status(self):
        self.core.tts_stream_status = 501
        real_client = httpx.AsyncClient

        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(self.core.handle), **kwargs)

        with patch.object(voice_routes.httpx, "AsyncClient", side_effect=client_factory):
            response = await self.client.post(
                "/api/chat/calls/call-a/tts/stream",
                json={"message_id": "reply-1", "generation": 7},
            )

        self.assertEqual(response.status_code, 503)

    async def test_stream_proxy_uses_core_contract_and_hides_core_receipts(self):
        paths = {route.path for route in self.router.routes}
        self.assertTrue(
            {
                "/api/chat/calls/{call_id}/audio/stream/start",
                "/api/chat/calls/{call_id}/audio/stream/chunk",
                "/api/chat/calls/{call_id}/audio/stream/finish",
                "/api/chat/calls/{call_id}/audio/stream/abort",
                "/api/chat/calls/{call_id}/transcribe",
            }.issubset(paths)
        )
        start = await self.client.post(self.path("start"), json={"generation": 7})
        self.assertEqual(start.status_code, 200)
        self.assertNotIn("core-stream-1", start.text)
        self.assertNotIn("unit-test-core-token", start.text)

        pcm_base64 = base64.b64encode(b"\x01\x00" * 1_600).decode("ascii")
        for seq in (0, 1):
            chunk = await self.client.post(
                self.path("chunk"),
                json={"generation": 7, "seq": seq, "pcm_base64": pcm_base64},
            )
            self.assertEqual(chunk.status_code, 200, chunk.text)
            self.assertEqual(chunk.json(), {"seq": seq, "generation": 7})
        duplicate = await self.client.post(
            self.path("chunk"),
            json={"generation": 7, "seq": 0, "pcm_base64": pcm_base64},
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(duplicate.json(), {"seq": 0, "generation": 7})

        finish = await self.client.post(self.path("finish"), json={"generation": 7})
        self.assertEqual(finish.status_code, 200, finish.text)
        self.assertEqual(finish.json(), {"text": "recognized words", "generation": 7})
        self.assertNotIn("receipt-secret", finish.text)
        self.assertNotIn("core-stream-1", finish.text)
        self.assertNotIn("unit-test-core-token", finish.text)

        self.assertEqual(
            [path.rsplit("/", 1)[-1] for path, _, _ in self.core.requests],
            ["start", "chunk", "chunk", "finish"],
        )
        for _, _, authorization in self.core.requests:
            self.assertEqual(authorization, "Bearer unit-test-core-token")
        self.assertEqual(
            self.core.requests[0][1],
            {"sample_rate": 16_000, "channels": 1, "platform": "webui"},
        )
        self.assertEqual(
            self.core.requests[1][1],
            {"stream_id": "core-stream-1", "seq": 0, "pcm_base64": pcm_base64},
        )
        self.assertEqual(self.core.requests[-1][1], {"stream_id": "core-stream-1"})

    async def test_sequence_conflicts_preserve_stream_but_over_64kb_aborts(self):
        start = await self.client.post(self.path("start"), json={"generation": 7})
        self.assertEqual(start.status_code, 200)
        pcm_base64 = base64.b64encode(b"\x00\x00").decode("ascii")
        out_of_order = await self.client.post(
            self.path("chunk"),
            json={
                "generation": 7,
                "seq": 1,
                "pcm_base64": pcm_base64,
            },
        )
        self.assertEqual(out_of_order.status_code, 409)
        self.assertEqual(len(self.proxy._streams), 1)
        self.assertFalse(any(path.endswith("/abort") for path, _, _ in self.core.requests))

        first = await self.client.post(
            self.path("chunk"),
            json={"generation": 7, "seq": 0, "pcm_base64": pcm_base64},
        )
        self.assertEqual(first.status_code, 200)
        duplicate = await self.client.post(
            self.path("chunk"),
            json={"generation": 7, "seq": 0, "pcm_base64": pcm_base64},
        )
        self.assertEqual(duplicate.status_code, 200)
        conflict = await self.client.post(
            self.path("chunk"),
            json={"generation": 7, "seq": 0, "pcm_base64": base64.b64encode(b"\x01\x00").decode("ascii")},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(self.proxy._streams), 1)
        chunk_requests = [path for path, _, _ in self.core.requests if path.endswith("/chunk")]
        self.assertEqual(len(chunk_requests), 1)

        oversized_pcm = b"\x00\x00" * ((64 * 1024 // 2) + 1)
        oversized = await self.client.post(
            self.path("chunk"),
            json={
                "generation": 7,
                "seq": 1,
                "pcm_base64": base64.b64encode(oversized_pcm).decode("ascii"),
            },
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(self.core.requests[-1][0].rsplit("/", 1)[-1], "abort")
        chunk_requests = [path for path, _, _ in self.core.requests if path.endswith("/chunk")]
        self.assertEqual(len(chunk_requests), 1)

    async def test_stale_generation_interrupt_and_end_abort_streams(self):
        await self.client.post(self.path("start"), json={"generation": 7})
        self.store.calls["call-a"]["generation"] = 8
        stale_finish = await self.client.post(self.path("finish"), json={"generation": 7})
        self.assertEqual(stale_finish.status_code, 409)
        self.assertEqual(self.core.requests[-1][0].rsplit("/", 1)[-1], "abort")
        self.assertNotIn("recognized words", stale_finish.text)

        await self.client.post(self.path("start"), json={"generation": 8})
        stale_chunk = await self.client.post(
            self.path("chunk"),
            json={
                "generation": 7,
                "seq": 0,
                "pcm_base64": base64.b64encode(b"\x00\x00").decode("ascii"),
            },
        )
        self.assertEqual(stale_chunk.status_code, 409)
        self.assertIn("call-a", self.proxy._streams)
        self.assertEqual(self.proxy._streams["call-a"].generation, 8)

        interrupted = await self.client.post(
            "/api/chat/calls/call-a/interrupt",
            json={"generation": 8},
        )
        self.assertEqual(interrupted.status_code, 200)
        self.assertEqual(interrupted.json(), {"generation": 9})
        self.assertEqual(self.core.requests[-1][0].rsplit("/", 1)[-1], "abort")

        await self.client.post(self.path("start"), json={"generation": 9})
        ended = await self.client.post("/api/chat/calls/call-a/end")
        self.assertEqual(ended.status_code, 200)
        self.assertEqual(self.core.requests[-1][0].rsplit("/", 1)[-1], "abort")

    async def test_proxy_shutdown_aborts_active_stream(self):
        await self.client.post(self.path("start"), json={"generation": 7})
        await self.proxy.aclose()
        self.assertEqual(self.core.requests[-1][0].rsplit("/", 1)[-1], "abort")
        self.assertIsNone(self.proxy._client)

    async def test_proxy_shutdown_drains_pending_start_and_aborts_late_stream(self):
        start_received = asyncio.Event()
        finish_start = asyncio.Event()

        class DelayedStartTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.operations: list[str] = []
                self.closed = False

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                operation = request.url.path.rsplit("/", 1)[-1]
                self.operations.append(operation)
                if operation == "start":
                    start_received.set()
                    await finish_start.wait()
                    return httpx.Response(200, json={"stream_id": "late-core-stream"})
                if operation == "abort":
                    return httpx.Response(200, json={"aborted": True})
                return httpx.Response(404, json={"detail": "unknown"})

            async def aclose(self) -> None:
                self.closed = True

        transport = DelayedStartTransport()
        proxy = voice_routes.CoreAudioStreamProxy(
            client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs)
        )
        starting = asyncio.create_task(proxy.start_stream("call-closing", 3))
        await asyncio.wait_for(start_received.wait(), timeout=1)

        closing = asyncio.create_task(proxy.aclose())
        # Let aclose mark the generation stale and begin waiting for start to
        # drain before allowing Core to return its newly-created stream ID.
        await asyncio.sleep(0)
        self.assertTrue(proxy._closing)
        self.assertFalse(transport.closed)
        finish_start.set()

        with self.assertRaises(voice_routes.VoiceCallError):
            await asyncio.wait_for(starting, timeout=1)
        await asyncio.wait_for(closing, timeout=1)

        self.assertEqual(transport.operations, ["start", "abort"])
        self.assertEqual(proxy._pending_starts, 0)
        self.assertTrue(transport.closed)

    async def test_proxy_caps_concurrent_stream_state_and_expires_idle_streams(self):
        for index in range(8):
            call_id = f"call-{index}"
            self.store.calls[call_id] = {"id": call_id, "status": "active", "generation": 1}
            response = await self.client.post(
                f"/api/chat/calls/{call_id}/audio/stream/start",
                json={"generation": 1},
            )
            self.assertEqual(response.status_code, 200, response.text)
        self.store.calls["call-overflow"] = {
            "id": "call-overflow",
            "status": "active",
            "generation": 1,
        }
        overflow = await self.client.post(
            "/api/chat/calls/call-overflow/audio/stream/start",
            json={"generation": 1},
        )
        self.assertEqual(overflow.status_code, 429)
        self.assertEqual(len(self.proxy._streams), 8)

        session = self.proxy._streams["call-0"]
        session.last_activity -= voice_routes.ASR_STREAM_IDLE_TIMEOUT_SECONDS + 1
        expired = await self.client.post(
            "/api/chat/calls/call-0/audio/stream/chunk",
            json={
                "generation": 1,
                "seq": 0,
                "pcm_base64": base64.b64encode(b"\x00\x00").decode("ascii"),
            },
        )
        self.assertEqual(expired.status_code, 409)
        self.assertNotIn("call-0", self.proxy._streams)
        self.assertEqual(self.core.requests[-1][0].rsplit("/", 1)[-1], "abort")

        session = self.proxy._streams["call-1"]
        session.created_at -= voice_routes.ASR_STREAM_MAX_DURATION_SECONDS + 1
        overlong = await self.client.post(
            "/api/chat/calls/call-1/audio/stream/finish",
            json={"generation": 1},
        )
        self.assertEqual(overlong.status_code, 409)
        self.assertNotIn("call-1", self.proxy._streams)
        self.assertEqual(self.core.requests[-1][0].rsplit("/", 1)[-1], "abort")

    async def test_readiness_accepts_core_remote_voice_route_without_local_runtime(self):
        self.tts.health = {
            "status": "degraded",
            "desired_profile": "full",
            "observed_local": {"perception": {"required": True, "ready": False}},
            "capabilities": {"perception": ["audio.transcribe.v1"]},
        }
        self.assertTrue(await voice_routes._asr_readiness(self.tts))
        self.tts.health["desired_profile"] = "potato"
        self.assertFalse(await voice_routes._asr_readiness(self.tts))


if __name__ == "__main__":
    unittest.main()
