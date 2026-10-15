import base64
import json
import logging
import unittest
from collections import deque
from unittest.mock import patch

import numpy as np

from core_audio_stream import (
    CoreAudioStreamClient,
    DiscordCoreAudioStreamBridge,
    STREAM_CHUNK_BYTES,
)


class _FakeContent:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    async def read(self, limit):
        return self.payload[:limit]


class _FakeResponse:
    def __init__(self, payload=None, status=200):
        self.status = status
        self.content = _FakeContent(payload or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _FakeSession:
    def __init__(self, replies):
        self.closed = False
        self.replies = deque(replies)
        self.calls = []

    def post(self, url, *, json, allow_redirects):
        self.calls.append((url, json, allow_redirects))
        return _FakeResponse(*self.replies.popleft())

    async def close(self):
        self.closed = True


class _FakeStreamClient:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.chunks = []
        self.aborts = []
        self.finished = []
        self.scopes = []

    async def start_stream(self, scope):
        if self.fail_at == "start":
            raise RuntimeError("start failed")
        self.scopes.append(scope)
        return "core-stream-1"

    async def send_chunk(self, stream_id, seq, pcm):
        if self.fail_at == "chunk":
            raise RuntimeError("chunk failed")
        self.chunks.append((stream_id, seq, pcm))

    async def finish_stream(self, stream_id):
        if self.fail_at == "finish":
            raise RuntimeError("finish failed")
        self.finished.append(stream_id)
        return {"text": "recognized", "result_id": "receipt-1"}

    async def abort_stream(self, stream_id):
        self.aborts.append(stream_id)


class CoreAudioStreamClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_exact_core_stream_contract_with_bearer_token(self):
        session = _FakeSession(
            [
                ({"stream_id": "core-1"}, 200),
                ({"seq": 3}, 200),
                ({"text": "hello", "result_id": "receipt-1"}, 200),
                ({}, 200),
            ]
        )
        client = CoreAudioStreamClient("core.internal", 8000, "secret-token")
        scope = "dsc_test_scope_1"
        with patch("core_audio_stream.aiohttp.ClientSession", return_value=session) as make_session:
            self.assertEqual(await client.start_stream(scope), "core-1")
            await client.send_chunk("core-1", 3, b"\x01\x00")
            result = await client.finish_stream("core-1")
            await client.abort_stream("core-1")
            await client.close()

        self.assertEqual(result["result_id"], "receipt-1")
        make_session.assert_called_once()
        self.assertEqual(
            make_session.call_args.kwargs["headers"],
            {"Authorization": "Bearer secret-token"},
        )
        self.assertTrue(session.closed)
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "http://core.internal:8000/api/multimodal/audio/stream/start",
                "http://core.internal:8000/api/multimodal/audio/stream/chunk",
                "http://core.internal:8000/api/multimodal/audio/stream/finish",
                "http://core.internal:8000/api/multimodal/audio/stream/abort",
            ],
        )
        start_body = session.calls[0][1]
        self.assertEqual(
            start_body,
            {
                "sample_rate": 16_000,
                "channels": 1,
                "platform": "discord",
                "scope": scope,
            },
        )
        self.assertNotIn("model", start_body)
        self.assertEqual(
            session.calls[1][1],
            {
                "stream_id": "core-1",
                "seq": 3,
                "pcm_base64": base64.b64encode(b"\x01\x00").decode("ascii"),
            },
        )

    async def test_downmixes_and_resamples_in_bounded_160ms_chunks(self):
        client = _FakeStreamClient()
        bridge = DiscordCoreAudioStreamBridge(client, logging.getLogger("test"))
        scope = "dsc_capture_a"
        self.assertTrue(await bridge.start("capture-a", scope))
        self.assertEqual(client.scopes, [scope])
        input_pcm = np.full((7_680, 2), 1200, dtype=np.int16).tobytes()

        self.assertTrue(await bridge.send_pcm("capture-a", input_pcm))
        self.assertEqual(len(client.chunks), 1)
        stream_id, seq, chunk = client.chunks[0]
        self.assertEqual((stream_id, seq), ("core-stream-1", 0))
        self.assertEqual(len(chunk), STREAM_CHUNK_BYTES)
        self.assertTrue(np.all(np.frombuffer(chunk, dtype=np.int16) == 1200))
        self.assertEqual(await bridge.finish("capture-a"), "receipt-1")
        self.assertEqual(client.finished, ["core-stream-1"])

    async def test_start_chunk_and_finish_failures_leave_no_receipt(self):
        for failed_stage in ("start", "chunk", "finish"):
            with self.subTest(failed_stage=failed_stage):
                client = _FakeStreamClient(fail_at=failed_stage)
                bridge = DiscordCoreAudioStreamBridge(
                    client, logging.getLogger("test")
                )
                started = await bridge.start("capture-a", "dsc_capture_a")
                self.assertEqual(started, failed_stage != "start")
                if started:
                    pcm = np.full((7_680, 2), 800, dtype=np.int16).tobytes()
                    sent = await bridge.send_pcm("capture-a", pcm)
                    if failed_stage == "chunk":
                        self.assertFalse(sent)
                    self.assertIsNone(await bridge.finish("capture-a"))
                else:
                    self.assertIsNone(await bridge.finish("capture-a"))
                self.assertNotIn("capture-a", bridge._streams)
                if failed_stage != "start":
                    self.assertEqual(client.aborts, ["core-stream-1"])


if __name__ == "__main__":
    unittest.main()
