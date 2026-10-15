import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from src.multimodal.client import LocalMultimodalClient
from src.multimodal.contracts import (
    TTSStreamChunk,
    TTSStreamError,
    TTSStreamSpec,
)
from src.multimodal.router import CoreMultimodalRouter


_VALID_HEADERS = {
    "X-Audio-Sample-Rate": "24000",
    "X-Audio-Channels": "1",
    "X-Audio-Sample-Width": "2",
    "X-Audio-Codec": "pcm_s16le",
    "X-TTS-Stream-Version": "1",
}


class _ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, block_before=None):
        self.chunks = list(chunks)
        self.block_before = block_before
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.completed = False

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if index == self.block_before:
                self.blocked.set()
                await self.release.wait()
            yield chunk
        self.completed = True

    async def aclose(self):
        self.closed = True
        self.release.set()


def _client_for_stream(stream, *, headers=None, status=200):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers=headers if headers is not None else _VALID_HEADERS,
            stream=stream,
            request=request,
        )

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return LocalMultimodalClient(tts_endpoint="http://tts.test", client=transport), transport, requests


class TTSStreamClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_audio_chunk_is_yielded_before_body_completion(self):
        stream = _ByteStream([b"\x01\x02", b"\x03\x04"], block_before=1)
        local, transport, requests = _client_for_stream(stream)
        try:
            audio = local.synthesize_tts_stream("hello")
            first = await asyncio.wait_for(anext(audio), timeout=0.5)

            self.assertEqual(first.pcm_s16le, b"\x01\x02")
            self.assertFalse(stream.completed)
            self.assertFalse(stream.closed)
            self.assertEqual(requests[0].url.path, "/api/tts/stream")
            self.assertEqual(
                json.loads(requests[0].content),
                {"text": "hello", "platform": "core", "text_lang": None},
            )
            await audio.aclose()
            self.assertTrue(stream.closed)
        finally:
            await transport.aclose()

    async def test_arbitrary_transport_framing_is_reassembled_on_pcm_frames(self):
        pcm = bytes(range(1, 21))
        headers = {**_VALID_HEADERS, "X-Audio-Channels": "2"}
        stream = _ByteStream([pcm[:1], pcm[1:6], pcm[6:8], pcm[8:]])
        local, transport, _requests = _client_for_stream(stream, headers=headers)
        try:
            chunks = [chunk async for chunk in local.synthesize_tts_stream("hello")]
        finally:
            await transport.aclose()

        self.assertEqual(b"".join(chunk.pcm_s16le for chunk in chunks), pcm)
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk.pcm_s16le) % 4 == 0 for chunk in chunks))
        self.assertTrue(all(chunk.spec.channels == 2 for chunk in chunks))
        self.assertTrue(all(chunk.spec.sample_rate == 24_000 for chunk in chunks))

    async def test_rejects_missing_or_mismatched_stream_headers_before_audio(self):
        invalid_headers = (
            {key: value for key, value in _VALID_HEADERS.items() if key != "X-Audio-Channels"},
            {**_VALID_HEADERS, "X-Audio-Sample-Width": "4"},
            {**_VALID_HEADERS, "X-Audio-Codec": "wav"},
            {**_VALID_HEADERS, "X-TTS-Stream-Version": "2"},
        )
        for headers in invalid_headers:
            with self.subTest(headers=headers):
                stream = _ByteStream([b"\x01\x02"])
                local, transport, _requests = _client_for_stream(stream, headers=headers)
                try:
                    with self.assertRaises(TTSStreamError) as caught:
                        await anext(local.synthesize_tts_stream("hello"))
                finally:
                    await transport.aclose()
                self.assertFalse(caught.exception.audio_started)
                self.assertTrue(stream.closed)

    async def test_response_status_marks_stream_as_unsupported_before_audio(self):
        stream = _ByteStream([])
        local, transport, _requests = _client_for_stream(stream, status=501)
        try:
            with self.assertRaises(TTSStreamError) as caught:
                await anext(local.synthesize_tts_stream("hello"))
        finally:
            await transport.aclose()

        self.assertEqual(caught.exception.code, "unsupported")
        self.assertTrue(caught.exception.fallback_allowed)
        self.assertEqual(caught.exception.status_code, 501)
        self.assertTrue(stream.closed)

    async def test_byte_limit_marks_failure_after_preceding_audio(self):
        stream = _ByteStream([b"\x01\x02", b"\x03\x04", b"\x05\x06"])
        local, transport, _requests = _client_for_stream(stream)
        audio = local.synthesize_tts_stream("hello")
        try:
            first = await anext(audio)
            self.assertEqual(first.pcm_s16le, b"\x01\x02")
            with patch("src.multimodal.client.TTS_STREAM_MAX_BYTES", 4):
                second = await anext(audio)
                self.assertEqual(second.pcm_s16le, b"\x03\x04")
                with self.assertRaises(TTSStreamError) as caught:
                    await anext(audio)
        finally:
            await transport.aclose()

        self.assertEqual(caught.exception.code, "stream_too_large")
        self.assertTrue(caught.exception.audio_started)
        self.assertFalse(caught.exception.fallback_allowed)
        self.assertTrue(stream.closed)

    async def test_total_time_limit_closes_an_incomplete_response(self):
        stream = _ByteStream([b"\x01\x02", b"\x03\x04"], block_before=1)
        local, transport, _requests = _client_for_stream(stream)
        try:
            with patch("src.multimodal.client.TTS_STREAM_MAX_DURATION_SECONDS", 0.02):
                audio = local.synthesize_tts_stream("hello")
                await anext(audio)
                with self.assertRaises(TTSStreamError) as caught:
                    await anext(audio)
        finally:
            await transport.aclose()

        self.assertEqual(caught.exception.code, "midstream_failure")
        self.assertTrue(caught.exception.audio_started)
        self.assertTrue(stream.closed)

    async def test_cancellation_closes_the_open_response(self):
        stream = _ByteStream([b"\x01\x02", b"\x03\x04"], block_before=1)
        local, transport, _requests = _client_for_stream(stream)
        try:
            audio = local.synthesize_tts_stream("hello")
            await anext(audio)
            pending = asyncio.create_task(anext(audio))
            await asyncio.wait_for(stream.blocked.wait(), timeout=0.5)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        finally:
            await transport.aclose()

        self.assertTrue(stream.closed)

    async def test_buffered_tts_keeps_the_legacy_endpoint_and_payload(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=b"legacy-wav",
                request=request,
            )

        transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        local = LocalMultimodalClient(tts_endpoint="http://tts.test", client=transport)
        try:
            result = await local.synthesize_tts("hello", platform="qq", text_lang="zh")
        finally:
            await transport.aclose()

        self.assertEqual(requests[0].url.path, "/api/tts")
        self.assertEqual(
            json.loads(requests[0].content),
            {"text": "hello", "platform": "qq", "text_lang": "zh"},
        )
        self.assertEqual(result.audio_base64, "bGVnYWN5LXdhdg==")
        self.assertEqual(result.audio_format, "wav")


class TTSStreamRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_promotes_late_failure_and_closes_local_iterator(self):
        chunk = TTSStreamChunk(
            pcm_s16le=b"\x01\x02",
            spec=TTSStreamSpec(sample_rate=24_000, channels=1, sample_width=2),
        )

        class Local:
            closed = False

            async def synthesize_tts_stream(self, _text, **_kwargs):
                try:
                    yield chunk
                    raise TTSStreamError("unavailable")
                finally:
                    self.closed = True

        local = Local()
        router = CoreMultimodalRouter(profile="full", local=local)
        stream = router.synthesize_tts_stream("hello")

        self.assertEqual(await anext(stream), chunk)
        with self.assertRaises(TTSStreamError) as caught:
            await anext(stream)

        self.assertEqual(caught.exception.code, "unavailable")
        self.assertTrue(caught.exception.audio_started)
        self.assertFalse(caught.exception.fallback_allowed)
        self.assertTrue(local.closed)

    async def test_potato_never_opens_a_tts_stream(self):
        class Local:
            called = False

            async def synthesize_tts_stream(self, *_args, **_kwargs):
                self.called = True
                yield

        local = Local()
        stream = CoreMultimodalRouter(profile="potato", local=local).synthesize_tts_stream("hello")
        with self.assertRaises(TTSStreamError) as caught:
            await anext(stream)

        self.assertEqual(caught.exception.code, "text_only")
        self.assertTrue(caught.exception.fallback_allowed)
        self.assertFalse(local.called)


if __name__ == "__main__":
    unittest.main()
