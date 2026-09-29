"""The public Core TTS stream keeps PCM framing and pre-audio errors intact."""

from __future__ import annotations

import unittest

import httpx
from fastapi import FastAPI

from src.multimodal.api import create_multimodal_router
from src.multimodal.contracts import TTSStreamChunk, TTSStreamError, TTSStreamSpec


class _Service:
    def __init__(self, *, error: bool = False):
        self.error = error
        self.received = None
        self.closed = False

    async def synthesize_tts_stream(self, text, *, platform, text_lang):
        self.received = (text, platform, text_lang)
        try:
            if self.error:
                raise TTSStreamError("unsupported", status_code=501)
            spec = TTSStreamSpec(sample_rate=24_000, channels=1, sample_width=2)
            yield TTSStreamChunk(b"\x01\x00", spec)
            yield TTSStreamChunk(b"\x02\x00", spec)
        finally:
            self.closed = True


class TTSStreamApiTests(unittest.IsolatedAsyncioTestCase):
    async def _post(self, service):
        app = FastAPI()
        app.include_router(create_multimodal_router(service), prefix="/api/multimodal")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(
                "/api/multimodal/tts/stream",
                json={"text": "hello", "platform": "webui", "text_lang": "zh"},
            )

    async def test_streams_raw_pcm_and_mirrors_format(self):
        service = _Service()
        response = await self._post(service)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"\x01\x00\x02\x00")
        self.assertEqual(response.headers["x-tts-stream-version"], "1")
        self.assertEqual(response.headers["x-audio-sample-rate"], "24000")
        self.assertEqual(response.headers["x-audio-channels"], "1")
        self.assertEqual(response.headers["x-audio-sample-width"], "2")
        self.assertEqual(response.headers["x-audio-codec"], "pcm_s16le")
        self.assertEqual(service.received, ("hello", "webui", "zh"))
        self.assertTrue(service.closed)

    async def test_pre_audio_failure_is_http_error(self):
        service = _Service(error=True)
        response = await self._post(service)

        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["detail"], "unsupported")
        self.assertTrue(service.closed)


if __name__ == "__main__":
    unittest.main()
