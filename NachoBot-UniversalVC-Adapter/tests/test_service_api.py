"""Real-loopback HTTP/WebSocket API tests with injected VAD/model fakes."""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp.test_utils import TestServer

from universalvc_service.api import MANAGER_APP_KEY, SessionManager, create_app
from universalvc_service.client import (
    UniversalVCClient,
    UniversalVCServiceError,
)
from universalvc_service.engine import UniversalVCSessionEngine
from universalvc_service.service_config import load_service_settings


class StatefulFakeVAD:
    available = True

    def __init__(self, **_kwargs):
        self.is_speaking = False
        self._samples = []

    def feed(self, samples):
        import numpy as np

        values = np.asarray(samples, dtype="float32").reshape(-1)
        if values.size and float(np.max(np.abs(values))) > 0.1:
            self.is_speaking = True
            self._samples.append(values.copy())
            return []
        if self.is_speaking:
            self.is_speaking = False
            collected = np.concatenate(self._samples) if self._samples else values
            self._samples.clear()
            return [SimpleNamespace(samples=collected)]
        return []

    def reset(self):
        self.is_speaking = False
        self._samples.clear()


class FakeOutput:
    def __init__(self, *_args, **_kwargs):
        self.initialized = False
        self.calls = []
        self.end_gate = None
        self.end_started = asyncio.Event()

    def initialize(self):
        self.initialized = True

    async def play(self, path):
        self.calls.append(("play", path))

    async def start_voice_stream(self, data):
        self.calls.append(("start", data["stream_id"]))

    async def write_voice_stream_chunk(self, data):
        self.calls.append(("chunk", data["stream_id"], data["seq"]))

    async def end_voice_stream(self, data):
        self.calls.append(("end", data["stream_id"]))
        self.end_started.set()
        if self.end_gate is not None:
            await self.end_gate.wait()

    async def abort_voice_stream(self, data):
        self.calls.append(("abort", data["stream_id"]))

    async def stop_current(self):
        self.calls.append(("stop",))

    async def stop_and_pause(self):
        self.calls.append(("pause",))

    def resume(self):
        self.calls.append(("resume",))

    async def interrupt(self):
        self.calls.append(("interrupt",))

    async def stop(self):
        self.calls.append(("close",))


class UniversalVCServiceAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        model = root / "fake-vad.onnx"
        model.write_bytes(b"placeholder consumed only by availability check")
        settings = load_service_settings(environ={"NACHOBOT_UNIVERSALVC_TOKEN": "loopback-test-token"})
        self.settings = dataclasses.replace(
            settings,
            vad_model_path=model,
            session_idle_timeout_seconds=2.0,
            heartbeat_interval_seconds=0.05,
        )
        self.vads = []
        self.outputs = []

        def engine_factory(settings, options, **kwargs):
            def vad_factory(**vad_options):
                vad = StatefulFakeVAD(**vad_options)
                self.vads.append(vad)
                return vad

            def output_factory(*factory_args, **factory_kwargs):
                output = FakeOutput(*factory_args, **factory_kwargs)
                self.outputs.append(output)
                return output

            return UniversalVCSessionEngine(
                settings, options, vad_factory=vad_factory, output_factory=output_factory, **kwargs
            )

        self.app = create_app(self.settings, engine_factory=engine_factory, service_instance_id="api-test-instance")
        self.server = TestServer(self.app, host="127.0.0.1")
        await self.server.start_server()
        self.client = UniversalVCClient(str(self.server.make_url("/")).rstrip("/"), "loopback-test-token")
        self.addAsyncCleanup(self._cleanup)

    async def _cleanup(self):
        await self.client.close()
        await self.server.close()

    @staticmethod
    def _options(*, mode="vad", source_id="main"):
        return {
            "platform": "service-test",
            "scope": "test-session-scope",
            "mode": mode,
            "sources": [{
                "id": source_id,
                "input": "external",
                "format": "s16le",
                "sample_rate": 16_000,
                "channels": 1,
                "capture": {},
                "identity": {"mode": "fixed", "id": "tester", "name": "Tester"},
            }],
            "output": {"mode": "none"},
            "denoise": False,
            "speaker": False,
            "speaker_log": False,
            "max_duration_seconds": 1,
        }

    async def test_authenticated_health_and_vad_wav_disconnect_lifecycle(self):
        async with aiohttp.ClientSession(trust_env=False) as unauthenticated:
            async with unauthenticated.get(str(self.server.make_url("/v1/health"))) as response:
                self.assertEqual(response.status, 401)

        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer loopback-test-token"}, trust_env=False
        ) as http:
            async with http.get(str(self.server.make_url("/v1/health"))) as response:
                self.assertEqual(response.status, 200)
                health = await response.json()
        self.assertEqual(health["service_instance_id"], "api-test-instance")
        self.assertEqual(health["protocol_version"], 1)
        self.assertIn("vad", health["capabilities"])

        session = await self.client.create_session(self._options())
        self.assertEqual(session.service_instance_id, "api-test-instance")
        first = await session.feed_vad((b"\xff\x3f" * 320), source_id="main")
        self.assertTrue(first["speaking"])
        self.assertEqual(first["completed_pcm"], b"")
        ended = await session.feed_vad((b"\x00\x00" * 320), source_id="main")
        self.assertFalse(ended["speaking"])
        self.assertEqual(len(ended["completed_pcm"]), 640)
        wav = await session.encode_wav(ended["completed_pcm"], max_duration_seconds=1)
        self.assertTrue(wav.startswith("UklGR"))
        self.assertEqual(len(self.vads), 1)

        await session._ws.close()
        for _ in range(50):
            if not self.app[MANAGER_APP_KEY].sessions:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.app[MANAGER_APP_KEY].sessions, {})

    async def test_vad_preserves_multiple_completed_segment_boundaries(self):
        import numpy as np

        session = await self.client.create_session(self._options())
        vad = self.vads[-1]

        def split_segments(_samples):
            vad.is_speaking = False
            return [
                SimpleNamespace(samples=np.full(12, 0.25, dtype=np.float32)),
                SimpleNamespace(samples=np.full(20, -0.25, dtype=np.float32)),
            ]

        vad.feed = split_segments
        result = await session.feed_vad(b"\x00\x00" * 320)
        self.assertFalse(result["speaking"])
        self.assertEqual(len(result["completed_segments"]), 2)
        self.assertEqual(
            result["completed_pcm"], result["completed_segments"][0] + result["completed_segments"][1]
        )
        await session.close()

    async def test_duplicate_audio_sequence_is_rejected_and_session_stays_fenced(self):
        session = await self.client.create_session(self._options())
        await session.feed_vad(b"\x00\x00" * 320, source_id="main")  # seq 0
        request_id, future = await session._request("feed_vad", source_id="main")
        with self.assertRaises(UniversalVCServiceError) as caught:
            await session._write_binary({
                **session._common_header(),
                "type": "audio",
                "op": "feed_vad",
                "request_id": request_id,
                "source_id": "main",
                "seq": 0,
            }, b"\x00\x00" * 320)
            await session._await_response(future)
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertIsNone(session._terminal_error)
        next_value = await session.feed_vad(b"\x00\x00" * 320, source_id="main")
        self.assertFalse(next_value["speaking"])
        await session.close()

    async def test_sessions_are_isolated_and_http_interrupt_checks_generation(self):
        first = await self.client.create_session(self._options(source_id="a"))
        second = await self.client.create_session(self._options(source_id="b"))
        first_result = await first.feed_vad(b"\xff\x3f" * 320, source_id="a")
        second_result = await second.feed_vad(b"\x00\x00" * 320, source_id="b")
        self.assertTrue(first_result["speaking"])
        self.assertFalse(second_result["speaking"])
        self.assertEqual(len(self.vads), 2)
        with self.assertRaises(UniversalVCServiceError) as caught:
            await self.client._request_json(
                "POST",
                f"/v1/sessions/{first.session_id}/interrupt",
                payload={
                    "session_generation": "stale-generation",
                    "service_instance_id": first.service_instance_id,
                },
            )
        self.assertEqual(caught.exception.code, "stale_session")
        with self.assertRaises(UniversalVCServiceError) as malformed:
            await self.client._request_json(
                "POST",
                f"/v1/sessions/{first.session_id}/interrupt",
                payload=[],
            )
        self.assertEqual(malformed.exception.code, "invalid_request")
        await first.interrupt()
        await first.close()
        await second.close()

    async def test_pending_request_count_and_aggregate_pcm_memory_are_bounded(self):
        session = await self.client.create_session(self._options())
        waiting = []
        for _ in range(8):
            _request_id, future = await session._request(
                "encode_wav_begin", source_id="main", extra={"max_duration_seconds": 1}
            )
            waiting.append(future)
        _request_id, overflow = await session._request(
            "encode_wav_begin", source_id="main", extra={"max_duration_seconds": 1}
        )
        with self.assertRaises(UniversalVCServiceError) as caught:
            await session._await_response(overflow)
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(len(self.app[MANAGER_APP_KEY].sessions[session.session_id].pending), 8)
        await session.close()
        await asyncio.gather(*waiting, return_exceptions=True)

        with patch("universalvc_service.api.MAX_PENDING_AUDIO_BYTES", 8):
            session = await self.client.create_session(self._options())
            first_id, first_future = await session._request(
                "encode_wav_begin", source_id="main", extra={"max_duration_seconds": 1}
            )
            header = {
                **session._common_header(), "type": "audio", "op": "encode_wav_chunk",
                "request_id": first_id, "source_id": "main", "seq": 0,
            }
            await session._write_binary(header, b"123456")
            second_id, second_future = await session._request(
                "encode_wav_begin", source_id="main", extra={"max_duration_seconds": 1}
            )
            header = {**header, "request_id": second_id, "seq": 0}
            await session._write_binary(header, b"789")
            await asyncio.gather(first_future, second_future, return_exceptions=True)
            for _ in range(50):
                if session.session_id not in self.app[MANAGER_APP_KEY].sessions:
                    break
                await asyncio.sleep(0.02)
            self.assertNotIn(session.session_id, self.app[MANAGER_APP_KEY].sessions)

    async def test_large_wav_and_voice_stream_control_frames_use_public_client(self):
        options = self._options(source_id="only-source")
        options["max_duration_seconds"] = 5
        options["output"] = {"mode": "device", "device_name": "Fake output", "sample_rate": 48_000}
        session = await self.client.create_session(options)
        pcm = b"\x00\x00" * 40_000
        wav = await session.encode_wav(pcm, max_duration_seconds=5)
        self.assertTrue(wav.startswith("UklGR"))
        # The separate WAV sequence namespace advances across the two chunks;
        # live VAD input remains at its own first sequence.
        self.assertEqual((await session.feed_vad(b"\x00\x00" * 320, source_id="only-source"))["speaking"], False)
        self.assertEqual((await session.pause_audio())["status"], "paused")
        self.assertEqual((await session.resume_audio())["status"], "resumed")

        start = {
            "event": "start", "stream_id": "stream-one", "sample_rate": 16_000,
            "channels": 1, "sample_width": 2, "codec": "pcm_s16le",
            "parent_message_id": "message-one",
        }
        await session.play_audio({"type": "voice_stream", "data": start})
        chunk = {"event": "chunk", "stream_id": "stream-one", "seq": 0}
        await session.play_audio({
            "type": "voice_stream",
            "data": {**chunk, "audio_base64": "AQACAA=="},
        })
        end = {"event": "end", "stream_id": "stream-one", "seq": 1}
        drained = await session.play_audio({"type": "voice_stream", "data": end})
        self.assertEqual(drained["status"], "render_drained")

        abort_start = {**start, "stream_id": "stream-two", "parent_message_id": "message-two"}
        await session.play_audio({"type": "voice_stream", "data": abort_start})
        aborted = await session.play_audio({
            "type": "voice_stream", "data": {"event": "abort", "stream_id": "stream-two"},
        })
        self.assertEqual(aborted["status"], "aborted")
        calls = self.outputs[0].calls
        self.assertIn(("start", "stream-one"), calls)
        self.assertIn(("chunk", "stream-one", 0), calls)
        self.assertIn(("end", "stream-one"), calls)
        self.assertIn(("abort", "stream-two"), calls)
        await session.close()

    async def test_heartbeat_remains_live_while_device_drain_is_in_flight(self):
        options = self._options()
        options["output"] = {"mode": "device", "device_name": "Fake output", "sample_rate": 48_000}
        session = await self.client.create_session(options)
        output = self.outputs[0]
        output.end_gate = asyncio.Event()
        start = {
            "event": "start", "stream_id": "blocked-stream", "sample_rate": 16_000,
            "channels": 1, "sample_width": 2, "codec": "pcm_s16le",
            "parent_message_id": "blocked-message",
        }
        await session.play_audio({"type": "voice_stream", "data": start})
        end_task = asyncio.create_task(session.play_audio({
            "type": "voice_stream", "data": {"event": "end", "stream_id": "blocked-stream"},
        }))
        await asyncio.wait_for(output.end_started.wait(), timeout=1)
        manager = self.app[MANAGER_APP_KEY]
        manager.settings = dataclasses.replace(manager.settings, session_idle_timeout_seconds=0.12)
        for _ in range(4):
            await session._write_json({"type": "heartbeat", "op": "heartbeat"})
            await asyncio.sleep(0.05)
        self.assertIn(session.session_id, manager.sessions)
        output.end_gate.set()
        drained = await asyncio.wait_for(end_task, timeout=1)
        self.assertEqual(drained["status"], "render_drained")
        await session.close()

    async def test_websocket_binding_is_reserved_before_concurrent_handshakes(self):
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer loopback-test-token"}, trust_env=False
        ) as http:
            async with http.post(
                str(self.server.make_url("/v1/sessions")), json=self._options()
            ) as response:
                self.assertEqual(response.status, 201)
                created = await response.json()
        path = created["websocket_path"]
        ws_url = str(self.server.make_url(path)).replace("http://", "ws://", 1)

        async def connect():
            connection = aiohttp.ClientSession(trust_env=False)
            try:
                ws = await connection.ws_connect(
                    ws_url,
                    headers={"Authorization": "Bearer loopback-test-token"},
                    max_msg_size=4 * 1024 * 1024,
                )
                return connection, ws, 101
            except aiohttp.WSServerHandshakeError as exc:
                await connection.close()
                return connection, None, exc.status

        first, second = await asyncio.gather(connect(), connect())
        winners = [entry for entry in (first, second) if entry[2] == 101]
        losers = [entry for entry in (first, second) if entry[2] != 101]
        self.assertEqual(len(winners), 1)
        self.assertEqual([entry[2] for entry in losers], [409])
        await winners[0][1].close()
        await winners[0][0].close()
        for _ in range(50):
            if created["session_id"] not in self.app[MANAGER_APP_KEY].sessions:
                break
            await asyncio.sleep(0.02)
        self.assertNotIn(created["session_id"], self.app[MANAGER_APP_KEY].sessions)


if __name__ == "__main__":
    unittest.main()
