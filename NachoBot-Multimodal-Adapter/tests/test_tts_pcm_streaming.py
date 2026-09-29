import asyncio
from pathlib import Path
import sys
import unittest

from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import TTSPipeline, WebUITTSRequest  # noqa: E402
from nachobot_multimodal.tts.base import PCMChunk  # noqa: E402


class _PCMModel:
    _initialized = True
    emotion_ready = True

    def __init__(self, *, release=None):
        self.release = release
        self.stream_calls = []
        self.stream_finished = False
        self.stream_closed = False

    async def tts(self, **kwargs):
        return b"buffered-wav"

    async def tts_pcm_stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        try:
            yield PCMChunk(b"\x01\x00\x02\x00", sample_rate=24000)
            if self.release is not None:
                await self.release.wait()
            self.stream_finished = True
            yield PCMChunk(b"\x03\x00\x04\x00", sample_rate=24000)
        finally:
            self.stream_closed = True


class PublicPCMStreamingTests(unittest.TestCase):
    def _pipeline(self, model=None):
        return TTSPipeline(
            PROJECT_ROOT / "configs" / "base.toml",
            backend="Vox",
            model=model or _PCMModel(),
        )

    def _endpoint(self, pipeline):
        return next(route.endpoint for route in pipeline.app.routes if route.path == "/api/tts/stream")

    def test_public_pcm_headers_and_first_chunk_arrive_before_generation_finishes(self):
        async def scenario():
            release = asyncio.Event()
            model = _PCMModel(release=release)
            pipeline = self._pipeline(model)
            response = await self._endpoint(pipeline)(WebUITTSRequest(text="hello", platform="webui"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["X-Audio-Sample-Rate"], "24000")
            self.assertEqual(response.headers["X-Audio-Channels"], "1")
            self.assertEqual(response.headers["X-Audio-Sample-Width"], "2")
            self.assertEqual(response.headers["X-Audio-Codec"], "pcm_s16le")
            self.assertEqual(response.headers["X-TTS-Stream-Version"], "1")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertTrue(pipeline._webui_tts_lock.locked())
            self.assertFalse(model.stream_finished)

            first = await response.body_iterator.__anext__()
            self.assertEqual(first, b"\x01\x00\x02\x00")
            self.assertFalse(model.stream_finished)
            release.set()
            second = await response.body_iterator.__anext__()
            self.assertEqual(second, b"\x03\x00\x04\x00")
            with self.assertRaises(StopAsyncIteration):
                await response.body_iterator.__anext__()
            self.assertTrue(model.stream_finished)
            self.assertTrue(model.stream_closed)
            self.assertFalse(pipeline._webui_tts_lock.locked())
            self.assertEqual(model.stream_calls, [{"text": "hello", "platform": "webui", "text_lang": None}])

        asyncio.run(scenario())

    def test_post_process_rejects_stream_before_backend_audio(self):
        async def scenario():
            model = _PCMModel()
            pipeline = self._pipeline(model)
            pipeline.config.base_config.tts_base_config.post_process = True
            with self.assertRaises(HTTPException) as caught:
                await self._endpoint(pipeline)(WebUITTSRequest(text="hello"))
            self.assertEqual(caught.exception.status_code, 501)
            self.assertEqual(model.stream_calls, [])
            self.assertFalse(pipeline._webui_tts_lock.locked())

        asyncio.run(scenario())

    def test_empty_stream_is_an_error_and_releases_the_model_lock(self):
        class EmptyModel(_PCMModel):
            async def tts_pcm_stream(self, **kwargs):
                self.stream_calls.append(kwargs)
                if False:
                    yield PCMChunk(b"", sample_rate=24000)
                self.stream_closed = True

        async def scenario():
            model = EmptyModel()
            pipeline = self._pipeline(model)
            with self.assertRaises(HTTPException) as caught:
                await self._endpoint(pipeline)(WebUITTSRequest(text="empty"))
            self.assertEqual(caught.exception.status_code, 502)
            self.assertTrue(model.stream_closed)
            self.assertFalse(pipeline._webui_tts_lock.locked())

        asyncio.run(scenario())

    def test_asgi_send_cancellation_closes_stream_and_releases_lock(self):
        async def scenario():
            release = asyncio.Event()
            model = _PCMModel(release=release)
            pipeline = self._pipeline(model)
            response = await self._endpoint(pipeline)(WebUITTSRequest(text="cancel"))
            send_started = asyncio.Event()
            hold_send = asyncio.Event()

            async def receive():
                await asyncio.Event().wait()

            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    send_started.set()
                    await hold_send.wait()

            response_task = asyncio.create_task(
                response(
                    {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}},
                    receive,
                    send,
                )
            )
            await asyncio.wait_for(send_started.wait(), timeout=1)
            response_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await response_task
            self.assertTrue(model.stream_closed)
            self.assertFalse(pipeline._webui_tts_lock.locked())

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
