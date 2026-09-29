from __future__ import annotations

import asyncio
import base64
import json
import threading
import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse

from nachobot_multimodal.api_server import (
    AudioStreamBody,
    AudioStreamChunkBody,
    AudioStreamStartBody,
    _decode_pcm_base64,
)
from nachobot_multimodal.local_runtime import (
    AudioStreamConflict,
    AudioStreamInferenceError,
    LocalBusy,
    LocalMultimodalRuntime,
    RuntimeUnavailable,
    UnsupportedOperation,
)


class FakeStreamingASR:
    supports_streaming = True
    model_identifier = "zh-xlarge-int8-2025-06-30"

    def __init__(self):
        self.streams = {}
        self.started = []
        self.accepted = []
        self.finished = []
        self.aborted = []
        self.fail_chunk = False
        self.fail_finish = False
        self.final_text = "final transcript"
        self.chunk_started = None
        self.chunk_release = None
        self.abort_started = None
        self.abort_release = None

    def start_stream(self, stream_id):
        self.started.append(stream_id)
        self.streams[stream_id] = []
        return True

    def accept_stream_audio(self, stream_id, samples):
        if self.fail_chunk:
            raise RuntimeError("chunk decode failed")
        if self.chunk_started is not None:
            self.chunk_started.set()
            self.chunk_release.wait(2)
        self.streams[stream_id].append(samples.copy())
        self.accepted.append(stream_id)
        return f"partial {len(self.streams[stream_id])}"

    def finish_stream(self, stream_id):
        self.finished.append(stream_id)
        if self.fail_finish:
            raise RuntimeError("flush failed")
        self.streams.pop(stream_id, None)
        return self.final_text

    def abort_stream(self, stream_id):
        if self.abort_started is not None:
            self.abort_started.set()
            self.abort_release.wait(2)
        self.aborted.append(stream_id)
        self.streams.pop(stream_id, None)


class AudioStreamingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.asr = FakeStreamingASR()
        self.runtime = LocalMultimodalRuntime(
            asr_transcriber=lambda _data: "full transcript",
            asr_streamer=self.asr,
            image_captioner=lambda _data: "caption",
        )
        await self.runtime.preload()

    async def asyncTearDown(self):
        await self.runtime.close_audio_streams()

    async def start(self):
        return await self.runtime.start_audio_stream(
            model=self.asr.model_identifier,
            sample_rate=16000,
            channels=1,
        )

    async def test_capability_is_advertised_only_for_loaded_streaming_recognizer(self):
        capabilities = self.runtime.capabilities().to_dict()
        self.assertTrue(capabilities["streaming_asr"])
        self.assertEqual(
            capabilities["models"]["audio.transcribe.v1"],
            self.asr.model_identifier,
        )

        no_stream = LocalMultimodalRuntime(
            asr_transcriber=lambda _data: "full transcript",
            image_captioner=lambda _data: "caption",
        )
        await no_stream.preload()
        self.assertFalse(no_stream.capabilities().to_dict()["streaming_asr"])

    async def test_start_requires_exact_loaded_model_and_pcm_format(self):
        with self.assertRaises(UnsupportedOperation):
            await self.runtime.start_audio_stream(
                model="another-model",
                sample_rate=16000,
                channels=1,
            )
        with self.assertRaises(ValueError):
            await self.runtime.start_audio_stream(
                model=self.asr.model_identifier,
                sample_rate=48000,
                channels=1,
            )
        self.assertEqual(self.asr.started, [])

    async def test_sequence_gaps_conflict_and_duplicates_do_not_redecode(self):
        stream_id = await self.start()
        chunk = b"\x00\x00" * 16

        self.assertEqual(
            await self.runtime.accept_audio_stream_chunk(
                stream_id=stream_id,
                seq=0,
                pcm_bytes=chunk,
            ),
            "partial 1",
        )
        with self.assertRaises(AudioStreamConflict):
            await self.runtime.accept_audio_stream_chunk(
                stream_id=stream_id,
                seq=2,
                pcm_bytes=chunk,
            )
        self.assertEqual(
            await self.runtime.accept_audio_stream_chunk(
                stream_id=stream_id,
                seq=1,
                pcm_bytes=chunk,
            ),
            "partial 2",
        )
        with self.assertRaises(AudioStreamConflict):
            await self.runtime.accept_audio_stream_chunk(
                stream_id=stream_id,
                seq=0,
                pcm_bytes=b"\x01\x00" * 16,
            )
        # Any earlier accepted sequence is safe to retry: its samples are not
        # fed to the recognizer again, and the current partial is replayed.
        self.assertEqual(
            await self.runtime.accept_audio_stream_chunk(
                stream_id=stream_id,
                seq=0,
                pcm_bytes=chunk,
            ),
            "partial 2",
        )
        self.assertEqual(len(self.asr.accepted), 2)

    async def test_shutdown_cannot_be_reopened_by_a_reaper_start_race(self):
        await self.runtime.close_audio_streams()

        with self.assertRaises(RuntimeUnavailable):
            await self.runtime.start_audio_stream_reaper()
        self.assertTrue(self.runtime._audio_streams_closing)

    async def test_pcm_validation_and_unknown_stream_do_not_call_recognizer(self):
        stream_id = await self.start()
        with self.assertRaises(ValueError):
            await self.runtime.accept_audio_stream_chunk(
                stream_id=stream_id,
                seq=0,
                pcm_bytes=b"\x00",
            )
        with self.assertRaises(KeyError):
            await self.runtime.accept_audio_stream_chunk(
                stream_id="missing",
                seq=0,
                pcm_bytes=b"\x00\x00",
            )
        self.assertEqual(self.asr.accepted, [])

    async def test_chunk_count_limit_terminalizes_stream(self):
        stream_id = await self.start()
        session = self.runtime._audio_streams[stream_id]
        session.next_seq = self.runtime.AUDIO_STREAM_MAX_CHUNKS
        session.chunk_digests.extend(
            b"\x00" * (self.runtime.AUDIO_STREAM_MAX_CHUNKS * 16)
        )

        with self.assertRaises(AudioStreamConflict):
            await self.runtime.accept_audio_stream_chunk(
                stream_id=stream_id,
                seq=self.runtime.AUDIO_STREAM_MAX_CHUNKS,
                pcm_bytes=b"\x00\x00",
            )
        self.assertIn(stream_id, self.asr.aborted)
        with self.assertRaises(AudioStreamConflict):
            await self.runtime.finish_audio_stream(stream_id)

    async def test_chunk_and_finish_failures_are_terminal_and_never_return_partial_as_final(self):
        stream_id = await self.start()
        await self.runtime.accept_audio_stream_chunk(
            stream_id=stream_id,
            seq=0,
            pcm_bytes=b"\x00\x00",
        )
        self.asr.fail_finish = True

        with self.assertRaises(AudioStreamInferenceError) as raised:
            await self.runtime.finish_audio_stream(stream_id)
        self.assertEqual(raised.exception.stage, "finalization")
        with self.assertRaises(AudioStreamConflict):
            await self.runtime.finish_audio_stream(stream_id)
        self.assertIn(stream_id, self.asr.aborted)

        other_stream = await self.start()
        self.asr.fail_finish = False
        self.asr.fail_chunk = True
        with self.assertRaises(AudioStreamInferenceError) as raised:
            await self.runtime.accept_audio_stream_chunk(
                stream_id=other_stream,
                seq=0,
                pcm_bytes=b"\x00\x00",
            )
        self.assertEqual(raised.exception.stage, "chunk")
        with self.assertRaises(AudioStreamConflict):
            await self.runtime.accept_audio_stream_chunk(
                stream_id=other_stream,
                seq=0,
                pcm_bytes=b"\x00\x00",
            )

    async def test_empty_final_is_successful_and_does_not_fall_back_to_partial(self):
        stream_id = await self.start()
        await self.runtime.accept_audio_stream_chunk(
            stream_id=stream_id,
            seq=0,
            pcm_bytes=b"\x00\x00",
        )
        self.asr.final_text = ""

        self.assertEqual(await self.runtime.finish_audio_stream(stream_id), "")
        self.assertEqual(await self.runtime.finish_audio_stream(stream_id), "")
        self.assertEqual(self.asr.finished.count(stream_id), 1)

    async def test_concurrency_duration_idle_expiry_and_shutdown_are_bounded(self):
        stream_ids = [await self.start() for _ in range(8)]
        with self.assertRaises(LocalBusy):
            await self.start()

        for seq in range(29):
            await self.runtime.accept_audio_stream_chunk(
                stream_id=stream_ids[0],
                seq=seq,
                pcm_bytes=b"\x00\x00" * (64 * 1024 // 2),
            )
        with self.assertRaises(ValueError):
            await self.runtime.accept_audio_stream_chunk(
                stream_id=stream_ids[0],
                seq=29,
                pcm_bytes=b"\x00\x00" * (20_000 // 2),
            )

        self.runtime.AUDIO_STREAM_IDLE_SECONDS = 0
        await self.runtime.expire_idle_audio_streams()
        with self.assertRaises(KeyError):
            await self.runtime.finish_audio_stream(stream_ids[1])
        self.assertIn(stream_ids[1], self.asr.aborted)

        remaining = [
            stream_id
            for stream_id in stream_ids
            if stream_id not in self.asr.aborted
        ]
        await self.runtime.close_audio_streams()
        self.assertTrue(set(remaining).issubset(set(self.asr.aborted)))
        self.assertEqual(self.asr.streams, {})

    async def test_reaper_expires_idle_streams_without_another_request(self):
        self.runtime.AUDIO_STREAM_IDLE_SECONDS = 0.01
        self.runtime.AUDIO_STREAM_SWEEP_SECONDS = 0.01
        stream_id = await self.start()
        await asyncio.sleep(0.05)

        with self.assertRaises(KeyError):
            await self.runtime.finish_audio_stream(stream_id)
        self.assertIn(stream_id, self.asr.aborted)

    async def test_cancellation_drains_worker_and_makes_chunk_retry_idempotent(self):
        stream_id = await self.start()
        self.asr.chunk_started = threading.Event()
        self.asr.chunk_release = threading.Event()
        chunk_call = asyncio.create_task(
            self.runtime.accept_audio_stream_chunk(
                stream_id=stream_id,
                seq=0,
                pcm_bytes=b"\x00\x00",
            )
        )
        self.assertTrue(await asyncio.to_thread(self.asr.chunk_started.wait, 1))
        chunk_call.cancel()
        await asyncio.sleep(0)
        self.assertFalse(chunk_call.done())

        self.asr.chunk_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await chunk_call
        self.assertEqual(
            await self.runtime.accept_audio_stream_chunk(
                stream_id=stream_id,
                seq=0,
                pcm_bytes=b"\x00\x00",
            ),
            "partial 1",
        )
        self.assertEqual(len(self.asr.accepted), 1)

    async def test_repeated_cancellation_does_not_interrupt_abort_cleanup(self):
        stream_id = await self.start()
        self.asr.abort_started = threading.Event()
        self.asr.abort_release = threading.Event()
        abort_call = asyncio.create_task(self.runtime.abort_audio_stream(stream_id))
        self.assertTrue(await asyncio.to_thread(self.asr.abort_started.wait, 1))
        abort_call.cancel()
        await asyncio.sleep(0)
        abort_call.cancel()
        await asyncio.sleep(0)
        self.asr.abort_release.set()

        with self.assertRaises(asyncio.CancelledError):
            await abort_call
        self.assertIn(stream_id, self.asr.aborted)
        self.assertEqual(self.asr.streams, {})
        self.assertIsNone(await self.runtime.abort_audio_stream(stream_id))


class AudioStreamingApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from nachobot_multimodal import api_server

        self.api = api_server
        self.asr = FakeStreamingASR()
        self.runtime = LocalMultimodalRuntime(
            asr_transcriber=lambda _data: "full transcript",
            asr_streamer=self.asr,
            image_captioner=lambda _data: "caption",
        )
        await self.runtime.preload()
        self.patch_runtime = patch.object(self.api, "_runtime", self.runtime)
        self.patch_runtime.start()

    async def asyncTearDown(self):
        self.patch_runtime.stop()
        await self.runtime.close_audio_streams()

    async def test_canonical_base64_and_exact_pcm_framing(self):
        self.assertEqual(_decode_pcm_base64("AAA="), b"\x00\x00")
        with self.assertRaises(ValueError):
            _decode_pcm_base64("AAB=")  # valid decoder input with non-canonical pad bits
        with self.assertRaises(ValueError):
            _decode_pcm_base64("AA==")  # one byte is not a complete s16le frame

    async def test_endpoint_contract_success_and_final_empty_text(self):
        started = await self.api.audio_stream_start(
            AudioStreamStartBody(
                model=self.asr.model_identifier,
                sample_rate=16000,
                channels=1,
            )
        )
        stream_id = started["stream_id"]
        chunk = await self.api.audio_stream_chunk(
            AudioStreamChunkBody(
                stream_id=stream_id,
                seq=0,
                pcm_base64=base64.b64encode(b"\x00\x00").decode(),
            )
        )
        self.assertEqual(chunk, {"seq": 0, "partial_text": "partial 1"})
        self.asr.final_text = ""
        self.assertEqual(
            await self.api.audio_stream_finish(AudioStreamBody(stream_id=stream_id)),
            {"text": ""},
        )

    async def test_api_errors_have_no_apparent_success_payload(self):
        mismatch = await self.api.audio_stream_start(
            AudioStreamStartBody(model="wrong", sample_rate=16000, channels=1)
        )
        self.assertIsInstance(mismatch, JSONResponse)
        self.assertEqual(mismatch.status_code, 501)

        started = await self.api.audio_stream_start(
            AudioStreamStartBody(
                model=self.asr.model_identifier,
                sample_rate=16000,
                channels=1,
            )
        )
        self.asr.fail_chunk = True
        response = await self.api.audio_stream_chunk(
            AudioStreamChunkBody(
                stream_id=started["stream_id"],
                seq=0,
                pcm_base64=base64.b64encode(b"\x00\x00").decode(),
            )
        )
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 502)
        payload = json.loads(response.body)
        self.assertEqual(payload["error"]["code"], "chunk_failure")
        self.assertNotIn("partial_text", payload)

    async def test_http_routes_validate_start_and_advertise_capability(self):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.api.app),
            base_url="http://test",
        ) as client:
            capabilities = await client.get("/v1/capabilities")
            self.assertTrue(capabilities.json()["streaming_asr"])

            missing_model = await client.post(
                "/v1/audio/stream/start",
                json={"sample_rate": 16000, "channels": 1},
            )
            self.assertEqual(missing_model.status_code, 400)
            self.assertEqual(
                missing_model.json()["error"]["code"],
                "invalid_request",
            )

            unsupported = await client.post(
                "/v1/audio/stream/start",
                json={"model": "wrong", "sample_rate": 16000, "channels": 1},
            )
            self.assertEqual(unsupported.status_code, 501)
            self.assertEqual(unsupported.json()["error"]["code"], "unsupported")


if __name__ == "__main__":
    unittest.main()
