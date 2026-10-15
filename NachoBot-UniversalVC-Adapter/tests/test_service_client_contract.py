from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from universalvc_service.client import (
    PROTOCOL_VERSION,
    UniversalVCClient,
    UniversalVCDisconnected,
    UniversalVCProtocolError,
    UniversalVCServiceError,
)


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-loopback-token"

_EXAMPLE_SPEC = importlib.util.spec_from_file_location(
    "_universalvc_service_client_example", ROOT / "examples" / "service_client.py"
)
assert _EXAMPLE_SPEC is not None and _EXAMPLE_SPEC.loader is not None
_EXAMPLE = importlib.util.module_from_spec(_EXAMPLE_SPEC)
sys.modules[_EXAMPLE_SPEC.name] = _EXAMPLE
_EXAMPLE_SPEC.loader.exec_module(_EXAMPLE)


class _FakeService:
    """A real loopback HTTP/WebSocket peer for the public client API."""

    def __init__(self) -> None:
        self.runner: web.AppRunner | None = None
        self.base_url = ""
        self.options: list[dict[str, Any]] = []
        self.auth_seen: list[str | None] = []
        self.websockets: dict[str, web.WebSocketResponse] = {}
        self.identities: dict[str, dict[str, Any]] = {}
        self.closed_sessions: list[str] = []
        self.interrupt_requests: list[tuple[str, dict[str, Any]]] = []
        self.requests: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self.frames: asyncio.Queue[tuple[str, dict[str, Any], bytes]] = asyncio.Queue()
        self.auto_audio_responses = True
        self.create_error: tuple[int, str, str] | None = None
        app = web.Application()
        app.router.add_post("/v1/sessions", self._create_session)
        app.router.add_get("/v1/sessions/{session_id}/stream", self._stream)
        app.router.add_delete("/v1/sessions/{session_id}", self._delete_session)
        app.router.add_post("/v1/sessions/{session_id}/interrupt", self._interrupt)
        self.app = app

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

    def _authorized(self, request: web.Request) -> bool:
        authorization = request.headers.get("Authorization")
        self.auth_seen.append(authorization)
        return authorization == f"Bearer {TOKEN}"

    async def _create_session(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response(
                {"error": {"code": "auth_required", "message": "token required"}},
                status=401,
            )
        if self.create_error is not None:
            status, code, message = self.create_error
            return web.json_response({"error": {"code": code, "message": message}}, status=status)
        value = await request.json()
        self.options.append(value)
        session_id = f"session-{len(self.options)}"
        self.identities[session_id] = {
            "protocol_version": PROTOCOL_VERSION,
            "service_instance_id": "service-test",
            "session_id": session_id,
            "session_generation": f"generation-{len(self.options)}",
        }
        return web.json_response(
            {
                **self.identities[session_id],
                "websocket_path": f"/v1/sessions/{session_id}/stream",
            }
        )

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        session_id = request.match_info["session_id"]
        identity = self.identities[session_id]
        websocket = web.WebSocketResponse(autoping=True, heartbeat=None)
        await websocket.prepare(request)
        self.websockets[session_id] = websocket
        await websocket.send_json({"type": "ready", **identity})
        try:
            async for message in websocket:
                if message.type == WSMsgType.TEXT:
                    value = json.loads(message.data)
                    if value.get("type") == "heartbeat":
                        continue
                    if value.get("type") != "request":
                        continue
                    await self.requests.put((session_id, value))
                    op = value.get("op")
                    if op in {"pause_audio", "resume_audio"}:
                        await self.respond(session_id, value["request_id"], {"accepted": True})
                    elif op == "play_voice_end":
                        await self.respond(session_id, value["request_id"], {"drained": True})
                elif message.type == WSMsgType.BINARY:
                    raw = message.data
                    if len(raw) < 4:
                        continue
                    header_length = struct.unpack(">I", raw[:4])[0]
                    header_end = 4 + header_length
                    if header_end > len(raw):
                        continue
                    header = json.loads(raw[4:header_end].decode("utf-8"))
                    pcm = raw[header_end:]
                    await self.frames.put((session_id, header, pcm))
                    if not self.auto_audio_responses:
                        continue
                    op = header.get("op")
                    request_id = header.get("request_id")
                    if op == "send_pcm":
                        await self.respond(session_id, request_id, {"source_id": header.get("source_id"), "bytes": len(pcm)})
                    elif op == "feed_vad":
                        completed = base64.b64encode(b"\x01\x00\x02\x00").decode("ascii")
                        await self.respond(
                            session_id,
                            request_id,
                            {"speaking": True, "completed_pcm": completed, "completed_segments": [completed]},
                        )
                    elif op == "encode_wav":
                        encoded = base64.b64encode(b"RIFFfake-WAVE-payload").decode("ascii")
                        await self.respond(session_id, request_id, {"wav_base64": encoded})
                    elif op == "play_voice_stream":
                        await self.respond(session_id, request_id, {"played": True})
        finally:
            self.websockets.pop(session_id, None)
        return websocket

    async def _delete_session(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": {"code": "auth_required", "message": "token required"}}, status=401)
        session_id = request.match_info["session_id"]
        self.closed_sessions.append(session_id)
        return web.json_response({"closed": True})

    async def _interrupt(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": {"code": "auth_required", "message": "token required"}}, status=401)
        session_id = request.match_info["session_id"]
        self.interrupt_requests.append((session_id, await request.json()))
        return web.json_response({"interrupted": True})

    async def respond(self, session_id: str, request_id: str, result: dict[str, Any]) -> None:
        await self.websockets[session_id].send_json(
            {
                "type": "response",
                **self.identities[session_id],
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
        )

    async def emit(self, session_id: str, event: str, **fields: Any) -> None:
        await self.websockets[session_id].send_json(
            {"type": "event", **self.identities[session_id], "event": event, **fields}
        )

    async def next_request(self, op: str | None = None) -> tuple[str, dict[str, Any]]:
        async def receive() -> tuple[str, dict[str, Any]]:
            while True:
                item = await self.requests.get()
                if op is None or item[1].get("op") == op:
                    return item

        return await asyncio.wait_for(receive(), timeout=2)

    async def next_frame(self, op: str | None = None) -> tuple[str, dict[str, Any], bytes]:
        async def receive() -> tuple[str, dict[str, Any], bytes]:
            while True:
                item = await self.frames.get()
                if op is None or item[1].get("op") == op:
                    return item

        return await asyncio.wait_for(receive(), timeout=2)


def _session_options() -> dict[str, Any]:
    return {
        "platform": "universal_vc",
        "scope": "test:session:explicit-options",
        "mode": "pipeline",
        "sources": [
            {
                "id": "main",
                "format": "f32le",
                "sample_rate": 48_000,
                "channels": 2,
                "input": "process",
                "capture": {
                    "target_process_name": "VRChat.exe",
                    "system_capture_device": "Loopback Device",
                    "device_name": "Application Audio",
                    "push_to_talk": False,
                    "ptt_key": "caps_lock",
                },
                "identity": {"mode": "speaker"},
            },
            {
                "id": "mic",
                "format": "s16le",
                "sample_rate": 16_000,
                "channels": 1,
                "input": "microphone",
                "capture": {"device_name": "USB Mic", "push_to_talk": True, "ptt_key": "caps_lock"},
                "identity": {"mode": "fixed", "id": "owner", "name": "Owner"},
            },
        ],
        "output": {"mode": "device", "device_name": "CABLE Input", "sample_rate": 44_100},
        "denoise": True,
        "speaker": True,
        "speaker_log": True,
        "vad": {"threshold": 0.55, "min_silence_duration": 0.25},
        "max_duration_seconds": 45,
    }


class UniversalVCServiceClientContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = _FakeService()
        await self.service.start()
        self.clients: list[UniversalVCClient] = []

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*(client.close() for client in self.clients), return_exceptions=True)
        await self.service.close()

    def make_client(self, token: str = TOKEN) -> UniversalVCClient:
        client = UniversalVCClient(self.service.base_url, token=token)
        self.clients.append(client)
        return client

    async def make_session(self, *, token: str = TOKEN, options: dict[str, Any] | None = None):
        return await self.make_client(token).create_session(options or _session_options())

    def test_import_does_not_load_native_audio_or_model_modules(self) -> None:
        code = (
            "import json,sys; import universalvc_service.client; "
            "names=('numpy','sherpa_onnx','sounddevice','proctap','proc_tap','onnxruntime'); "
            "print(json.dumps([n for n in names if n in sys.modules or any(k.startswith(n+'.') for k in sys.modules)]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), [])

    def test_cli_builder_requires_explicit_input_and_serializes_capture_flags(self) -> None:
        parser = _EXAMPLE._parser()
        with self.assertRaisesRegex(ValueError, "provide --process, --pid, --system-capture-device, or --input-device"):
            _EXAMPLE.build_session_options(parser.parse_args([]))

        args = parser.parse_args(
            [
                "--process", "TestApplication.exe",
                "--input-device", "USB Microphone",
                "--push-to-talk",
                "--ptt-key", "f9",
                "--output-device", "Virtual Cable",
                "--denoise", "--speaker", "--speaker-log",
            ]
        )
        options = _EXAMPLE.build_session_options(args)
        self.assertEqual(options["mode"], "pipeline")
        self.assertEqual(options["sources"][0]["capture"], {"target_process_name": "TestApplication.exe"})
        self.assertEqual(options["sources"][0]["identity"], {"mode": "speaker"})
        self.assertEqual(
            options["sources"][1]["capture"],
            {"device_name": "USB Microphone", "push_to_talk": True, "ptt_key": "f9"},
        )
        self.assertEqual(options["sources"][1]["identity"], {"mode": "fixed", "id": "owner", "name": "Owner"})
        self.assertEqual(options["output"], {"mode": "device", "device_name": "Virtual Cable", "sample_rate": 48_000})
        self.assertEqual((options["denoise"], options["speaker"], options["speaker_log"]), (True, True, True))

        system_args = parser.parse_args(["--system-capture-device", "System Input", "--speaker-log"])
        system_options = _EXAMPLE.build_session_options(system_args)
        self.assertEqual(system_options["sources"][0]["capture"], {"system_capture_device": "System Input"})
        self.assertEqual(system_options["sources"][0]["identity"], {"mode": "fixed", "id": "application", "name": "System Input"})
        self.assertEqual((system_options["speaker"], system_options["speaker_log"]), (False, True))

        pid_options = _EXAMPLE.build_session_options(parser.parse_args(["--pid", "1234"]))
        self.assertEqual(pid_options["sources"][0]["capture"], {"target_pid": 1234})
        self.assertEqual(pid_options["sources"][0]["identity"], {"mode": "fixed", "id": "application", "name": "PID 1234"})
        with self.assertRaisesRegex(ValueError, "choose either --process or --pid"):
            _EXAMPLE.build_session_options(parser.parse_args(["--process", "App.exe", "--pid", "1234"]))
        with self.assertRaisesRegex(ValueError, "choose a process selector or --system-capture-device"):
            _EXAMPLE.build_session_options(
                parser.parse_args(["--process", "App.exe", "--system-capture-device", "System Input"])
            )

    async def test_explicit_request_is_serialized_and_pcm_has_bounded_correlated_frames(self) -> None:
        options = _session_options()
        session = await self.make_session(options=options)
        self.assertEqual(self.service.options, [options])
        self.assertEqual(session.session_id, "session-1")
        self.assertEqual(session.session_generation, "generation-1")
        self.assertEqual(session.service_instance_id, "service-test")
        with self.assertRaisesRegex(ValueError, "source_id was not declared"):
            await session.send_pcm(b"undeclared", source_id="rogue-source")
        self.assertEqual(set(session._sequence_locks), {"main", "mic"})

        first_pcm = b"\x00\x01\x02\x03"
        first = asyncio.create_task(session.send_pcm(first_pcm, source_id="main"))
        session_id, request = await self.service.next_request("send_pcm")
        frame_session, header, payload = await self.service.next_frame("send_pcm")
        self.assertEqual(session_id, frame_session)
        self.assertEqual(payload, first_pcm)
        self.assertEqual(header["request_id"], request["request_id"])
        self.assertEqual(header["source_id"], "main")
        self.assertEqual(header["seq"], 0)
        self.assertEqual(header["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(header["session_generation"], session.session_generation)
        self.assertEqual(await first, {"source_id": "main", "bytes": len(first_pcm)})

        second_pcm = b"\x10\x11"
        second = asyncio.create_task(session.send_pcm(second_pcm, source_id="mic"))
        _, second_request = await self.service.next_request("send_pcm")
        _, second_header, second_payload = await self.service.next_frame("send_pcm")
        self.assertEqual(second_payload, second_pcm)
        self.assertEqual(second_header["request_id"], second_request["request_id"])
        self.assertEqual(second_header["source_id"], "mic")
        self.assertEqual(second_header["seq"], 0)
        self.assertEqual(await second, {"source_id": "mic", "bytes": len(second_pcm)})

    async def test_responses_are_correlated_when_concurrent_source_calls_finish_out_of_order(self) -> None:
        self.service.auto_audio_responses = False
        session = await self.make_session()
        main_task = asyncio.create_task(session.send_pcm(b"main-audio", "main"))
        mic_task = asyncio.create_task(session.send_pcm(b"mic-audio", "mic"))
        requests = [await self.service.next_request("send_pcm") for _ in range(2)]
        frames = [await self.service.next_frame("send_pcm") for _ in range(2)]
        self.assertEqual({item[1]["source_id"] for item in requests}, {"main", "mic"})
        for _sid, request in reversed(requests):
            await self.service.respond(request["session_id"], request["request_id"], {"source": request["source_id"]})
        main_result, mic_result = await asyncio.gather(main_task, mic_task)
        self.assertEqual(main_result, {"source": "main"})
        self.assertEqual(mic_result, {"source": "mic"})
        self.assertEqual(len(frames), 2)

    async def test_vad_wav_playback_interrupt_and_pause_calls_use_public_protocol(self) -> None:
        session = await self.make_session()
        pcm = b"\x01\x00\x02\x00"
        vad_task = asyncio.create_task(session.feed_vad(pcm, "main"))
        vad_request = (await self.service.next_request("feed_vad"))[1]
        vad_frame = (await self.service.next_frame("feed_vad"))[1]
        self.assertEqual(vad_frame["seq"], 0)
        self.assertEqual(vad_frame["request_id"], vad_request["request_id"])
        vad_result = await vad_task
        self.assertTrue(vad_result["speaking"])
        self.assertEqual(vad_result["completed_segments"], (pcm,))

        wav_task = asyncio.create_task(session.encode_wav(pcm, max_duration_seconds=2))
        wav_request = (await self.service.next_request("encode_wav"))[1]
        wav_frame = (await self.service.next_frame("encode_wav"))[1]
        self.assertEqual(wav_frame["seq"], 0)
        self.assertEqual(wav_frame["request_id"], wav_request["request_id"])
        encoded = await wav_task
        self.assertEqual(base64.b64decode(encoded), b"RIFFfake-WAVE-payload")
        live_task = asyncio.create_task(session.send_pcm(pcm, "main"))
        await self.service.next_request("send_pcm")
        live_frame = (await self.service.next_frame("send_pcm"))[1]
        self.assertEqual(live_frame["seq"], 1)
        await live_task

        voice = b"RIFFvoice-audio"
        voice_task = asyncio.create_task(session.play_audio({"type": "voice", "data": base64.b64encode(voice).decode()}))
        begin = (await self.service.next_request("play_voice_begin"))[1]
        voice_frame = (await self.service.next_frame("play_voice_chunk"))[1]
        end = (await self.service.next_request("play_voice_end"))[1]
        self.assertEqual(begin["audio_bytes"], len(voice))
        self.assertEqual(voice_frame["request_id"], begin["request_id"])
        self.assertEqual(voice_frame["seq"], 0)
        self.assertEqual(end["request_id"], begin["request_id"])
        self.assertEqual(await voice_task, {"drained": True})

        stream_data = {"event": "chunk", "stream_id": "stream-a", "seq": 0, "audio_base64": base64.b64encode(pcm).decode(), "sample_rate": 16_000, "channels": 1, "sample_width": 2, "codec": "pcm_s16le"}
        stream_task = asyncio.create_task(session.play_audio({"type": "voice_stream", "data": stream_data}))
        stream_request = (await self.service.next_request("play_voice_stream"))[1]
        stream_frame_session, stream_frame, stream_pcm = await self.service.next_frame("play_voice_stream")
        self.assertEqual(stream_frame_session, session.session_id)
        self.assertEqual(stream_frame["request_id"], stream_request["request_id"])
        self.assertEqual(stream_frame["segment"]["event"], "chunk")
        self.assertNotIn("audio_base64", stream_frame["segment"])
        self.assertEqual(stream_pcm, pcm)
        self.assertEqual(await stream_task, {"played": True})

        await session.pause_audio()
        await self.service.next_request("pause_audio")
        await session.resume_audio()
        await self.service.next_request("resume_audio")
        await session.interrupt()
        self.assertEqual(self.service.interrupt_requests, [(session.session_id, {
            "session_generation": session.session_generation,
            "service_instance_id": session.service_instance_id,
        })])

    async def test_events_preserve_session_identity_and_disconnect_is_reported(self) -> None:
        session = await self.make_session()
        await self.service.emit(session.session_id, "speech_result", source_id="main", turn_id="turn-1", speaker_id="speaker-1", speaker_name="Guest", precomputed_asr_text="hello")
        event = await asyncio.wait_for(anext(session.events()), timeout=1)
        self.assertEqual(event["event"], "speech_result")
        self.assertEqual(event["session_generation"], session.session_generation)
        self.assertEqual(event["source_id"], "main")
        self.assertEqual(event["turn_id"], "turn-1")
        self.service.auto_audio_responses = False
        pending = asyncio.create_task(session.send_pcm(b"blocked", "main"))
        await self.service.next_request("send_pcm")
        await self.service.next_frame("send_pcm")
        await self.service.websockets[session.session_id].close()
        with self.assertRaises(UniversalVCDisconnected):
            await asyncio.wait_for(pending, timeout=1)
        with self.assertRaises(UniversalVCDisconnected):
            await asyncio.wait_for(anext(session.events()), timeout=1)
        self.assertEqual(session._pending, {})

    async def test_wrong_generation_and_unknown_response_id_fail_closed(self) -> None:
        session = await self.make_session()
        stale = {**self.service.identities[session.session_id], "session_generation": "older-generation"}
        await self.service.websockets[session.session_id].send_json({"type": "event", **stale, "event": "speech_start"})
        with self.assertRaises(UniversalVCProtocolError):
            await asyncio.wait_for(anext(session.events()), timeout=1)

        second = await self.make_session()
        await self.service.websockets[second.session_id].send_json(
            {
                "type": "response",
                **self.service.identities[second.session_id],
                "request_id": "unknown-request-id",
                "ok": True,
                "result": {},
            }
        )
        with self.assertRaises(UniversalVCProtocolError):
            await asyncio.wait_for(anext(second.events()), timeout=1)

    async def test_event_queue_overflow_fails_the_session_instead_of_growing_unbounded(self) -> None:
        session = await self.make_session()
        for index in range(140):
            await self.service.emit(session.session_id, "speech_start", source_id="main", turn_id=f"turn-{index}")
        async def terminal_error() -> BaseException | None:
            deadline = asyncio.get_running_loop().time() + 2
            while session._terminal_error is None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.005)
            return session._terminal_error

        error = await terminal_error()
        self.assertIsInstance(error, UniversalVCProtocolError)
        self.assertLessEqual(session._events.qsize(), session._events.maxsize)

    async def test_http_auth_and_service_errors_are_preserved_as_safe_codes(self) -> None:
        no_token = self.make_client(token="")
        with self.assertRaises(UniversalVCServiceError) as missing_auth:
            await no_token.create_session(_session_options())
        self.assertEqual(missing_auth.exception.code, "auth_required")
        self.assertIsNone(self.service.auth_seen[-1])

        self.service.create_error = (422, "options_invalid", "session options rejected")
        with self.assertRaises(UniversalVCServiceError) as rejected:
            await self.make_client().create_session(_session_options())
        self.assertEqual(rejected.exception.code, "options_invalid")
        self.assertEqual(str(rejected.exception), "UniversalVC service error options_invalid: session options rejected")

    async def test_cancelled_concurrent_requests_release_pending_futures_and_source_locks(self) -> None:
        self.service.auto_audio_responses = False
        session = await self.make_session()
        active = asyncio.create_task(session.send_pcm(b"blocked", "main"))
        await self.service.next_request("send_pcm")
        await self.service.next_frame("send_pcm")
        waiters = [
            asyncio.create_task(session.send_pcm(b"waiting", "main" if i % 2 == 0 else "mic"))
            for i in range(24)
        ]
        await asyncio.sleep(0)
        for task in [active, *waiters]:
            task.cancel()
        await asyncio.gather(active, *waiters, return_exceptions=True)
        self.assertEqual(session._pending, {})
        self.assertLessEqual(len(session._sequence_locks), 2)

    async def test_session_close_deletes_server_lease_and_closes_client_resources(self) -> None:
        session = await self.make_session()
        await session.close()
        self.assertEqual(self.service.closed_sessions, [session.session_id])
        self.assertNotIn(session, session._client._sessions)
        await self.make_client().close()


if __name__ == "__main__":
    unittest.main()
