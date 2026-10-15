import asyncio
import base64
import io
import struct
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from adapter import BilibiliAdapter
from bili_src.audio.core_audio_stream import CoreAudioStreamClient
from bili_src.audio.mic_capture import (
    MicCaptureWorker,
    MicConfig,
    PCM16Mono16kResampler,
)


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class _PCMBuffer:
    def __init__(self, pcm):
        self.pcm = pcm

    def tobytes(self):
        return self.pcm


class _FakeSender:
    def __init__(self, mode):
        self.mode = mode
        self.chunks = []
        self.abort_called = False

    def enqueue_chunk(self, pcm):
        if self.mode == "chunk_failure":
            return False
        self.chunks.append(bytes(pcm))
        return True

    async def finish(self):
        if self.mode == "finish_failure":
            return None
        return {"text": "recognized speech", "result_id": "receipt-1"}

    async def abort(self):
        self.abort_called = True


class _FakeStreamClient:
    def __init__(self, mode="success"):
        self.mode = mode
        self.sender = _FakeSender(mode)
        self.open_payload = None

    async def open_stream(self, **kwargs):
        self.open_payload = kwargs
        if self.mode == "start_failure":
            raise RuntimeError("Core stream start unavailable")
        return self.sender

    async def close(self):
        pass


def _decode_wav(payload):
    with wave.open(io.BytesIO(payload), "rb") as wav:
        return wav.getframerate(), wav.getnchannels(), wav.readframes(wav.getnframes())


class MicAudioStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def _run_worker_events(self, mode, *, event="finish", platform="bilibili"):
        pcm = struct.pack("<4h", 100, -200, 300, -400)
        delivered = []

        async def on_recognized(*args):
            delivered.append(args)

        client = _FakeStreamClient(mode)
        worker = MicCaptureWorker(
            MicConfig(sample_rate=16_000, channels=1, platform=platform),
            on_recognized,
            _Logger(),
            stream_client=client,
        )
        await worker._process_stream_event("start", None)
        await worker._process_stream_event("audio", pcm)
        fallback = {
            "reason": "PTT released" if event == "finish" else "worker stopped",
            "pcm_chunks": (pcm,),
            "pcm_bytes": len(pcm),
        }
        await worker._process_stream_event(event, fallback)
        return client, delivered, pcm

    async def test_resamples_and_downmixes_to_mono_16khz(self):
        resampler = PCM16Mono16kResampler(48_000, 2)
        frames = [value for _ in range(4_800) for value in (1_000, 3_000)]
        pcm = struct.pack(f"<{len(frames)}h", *frames)

        converted = resampler.convert(pcm)

        self.assertEqual(len(converted), 1_600 * 2)
        output = struct.unpack(f"<{len(converted) // 2}h", converted)
        self.assertTrue(all(sample == 2_000 for sample in output))

    async def test_vad_keeps_preroll_and_ends_on_silence(self):
        delivered = []

        async def on_recognized(payload):
            delivered.append(payload)

        worker = MicCaptureWorker(
            MicConfig(
                sample_rate=16_000,
                channels=1,
                silence_threshold=0.01,
                silence_duration=0.1,
            ),
            on_recognized,
            _Logger(),
        )
        silence = b"\x00\x00" * 1_600
        speech = struct.pack("<1600h", *([1_000] * 1_600))
        worker._running = True
        worker._audio_callback(_PCMBuffer(silence), 1_600, None, None)
        worker._audio_callback(_PCMBuffer(speech), 1_600, None, None)
        worker._audio_callback(_PCMBuffer(silence), 1_600, None, None)
        worker._running = False

        await worker._process_queue_loop()

        self.assertEqual(len(delivered), 1)
        self.assertEqual(
            _decode_wav(delivered[0]),
            (16_000, 1, silence + speech + silence),
        )

    async def test_stream_transport_sends_ordered_core_contract_without_model(self):
        client = CoreAudioStreamClient("http://core-host:8000", token="secret")
        requests = []

        async def fake_post(path, payload):
            requests.append((path, dict(payload)))
            if path.endswith("/start"):
                return {"stream_id": "stream-1"}
            if path.endswith("/chunk"):
                return {"seq": payload["seq"]}
            if path.endswith("/finish"):
                return {"text": "recognized speech", "result_id": "receipt-1"}
            return {}

        client._post = fake_post
        sender = await client.open_stream(
            sample_rate=16_000, channels=1, platform="bilibili"
        )
        self.assertTrue(sender.enqueue_chunk(b"\x01\x00"))
        self.assertTrue(sender.enqueue_chunk(b"\x02\x00"))
        result = await sender.finish()

        self.assertEqual(result["result_id"], "receipt-1")
        self.assertEqual(
            [path.rsplit("/", 1)[-1] for path, _ in requests],
            ["start", "chunk", "chunk", "finish"],
        )
        self.assertEqual(
            requests[0][1],
            {"sample_rate": 16_000, "channels": 1, "platform": "bilibili"},
        )
        self.assertNotIn("model", requests[0][1])
        self.assertEqual(requests[1][1]["seq"], 0)
        self.assertEqual(
            base64.b64decode(requests[1][1]["pcm_base64"]), b"\x01\x00"
        )

    async def test_start_chunk_and_finish_failures_keep_complete_wav(self):
        for mode in ("start_failure", "chunk_failure", "finish_failure"):
            with self.subTest(mode=mode):
                client, delivered, pcm = await self._run_worker_events(mode)
                self.assertEqual(len(delivered), 1)
                wav_payload = delivered[0][0]
                self.assertEqual(_decode_wav(wav_payload), (16_000, 1, pcm))
                self.assertEqual(len(delivered[0]), 1)

    async def test_shutdown_abort_keeps_complete_wav(self):
        client, delivered, pcm = await self._run_worker_events(
            "success", event="abort"
        )

        self.assertTrue(client.sender.abort_called)
        self.assertEqual(_decode_wav(delivered[0][0]), (16_000, 1, pcm))
        self.assertEqual(len(delivered[0]), 1)

    async def test_receipt_is_passed_only_for_exact_bilibili_platform(self):
        for platform, expected_args in (("bilibili", 2), ("bilibili.live", 1)):
            with self.subTest(platform=platform):
                _, delivered, _ = await self._run_worker_events(
                    "success", platform=platform
                )
                self.assertEqual(len(delivered[0]), expected_args)

    async def test_receipt_is_attached_to_final_voice_only_for_bilibili(self):
        for platform, should_attach in (("bilibili", True), ("bilibili.live", False)):
            with self.subTest(platform=platform):
                adapter = BilibiliAdapter.__new__(BilibiliAdapter)
                adapter.config = SimpleNamespace(
                    platform=platform,
                    live_master_user_id="owner-1",
                    live_master_user_name="owner",
                )
                adapter.tts_manager = SimpleNamespace(reset_idle_timer=Mock())
                adapter._build_live_additional_config = lambda _room, extra: dict(extra)
                adapter._get_template_info = AsyncMock(return_value=object())
                sent = []

                async def send(message):
                    sent.append(message)

                adapter._send_to_nachobot = send
                wav_payload = b"complete wav bytes"
                await adapter.handle_mic_message(
                    42,
                    wav_payload,
                    precomputed_asr_result_id="receipt-1",
                )
                await asyncio.sleep(0)

                self.assertEqual(len(sent), 1)
                message = sent[0]
                additional = message.message_info.additional_config
                self.assertEqual(
                    additional.get("precomputed_asr_result_id"),
                    "receipt-1" if should_attach else None,
                )
                self.assertEqual(
                    base64.b64decode(message.message_segment.data), wav_payload
                )


if __name__ == "__main__":
    unittest.main()
