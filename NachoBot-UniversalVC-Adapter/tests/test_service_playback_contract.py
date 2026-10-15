"""Per-session playback lifecycle tests without opening a native output device."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import logging
import tempfile
import unittest
import wave
from pathlib import Path

from universalvc_service.audio_output import AudioOutput
from universalvc_service.config import AudioOutputConfig
from universalvc_service.engine import SessionRequestError, UniversalVCSessionEngine
from universalvc_service.service_config import load_service_settings


class FakePipeline:
    def __init__(self, config, _logger, **_callbacks):
        self.vad = type("FakeVAD", (), {"available": True})()
        self.denoiser = type("FakeDenoiser", (), {"enabled": config.denoise.enabled})()
        self.speaker_tracker = type("FakeSpeaker", (), {"enabled": config.speaker.enabled})()

    def set_loop(self, _loop):
        pass

    async def interrupt(self):
        pass

    async def stop_streaming(self):
        pass


class FakeCore:
    async def close(self):
        pass


class FakeOutput:
    """Records the engine protocol while allowing deterministic drain races."""

    def __init__(self, config, _logger, **_kwargs):
        self.config = config
        self.initialized = False
        self.buffered_replies: list[str] = []
        self.active_stream: dict | None = None
        self.replaced_streams: list[str] = []
        self.chunks: list[dict] = []
        self.aborted_streams: list[str] = []
        self.end_started = asyncio.Event()
        self.allow_end = asyncio.Event()
        self.block_end = False
        self.fail_chunk = False
        self.fail_end = False
        self.interrupt_count = 0
        self.stop_current_count = 0
        self.stopped = False

    def initialize(self):
        self.initialized = True

    async def play(self, wav_path):
        self.buffered_replies.append(wav_path)

    async def start_voice_stream(self, metadata):
        if self.active_stream is not None:
            self.replaced_streams.append(self.active_stream["stream_id"])
        self.active_stream = dict(metadata)

    async def write_voice_stream_chunk(self, data):
        if self.fail_chunk:
            raise RuntimeError("fake output chunk write failed")
        if self.active_stream is None:
            raise RuntimeError("chunk without active output stream")
        self.chunks.append(dict(data))

    async def end_voice_stream(self, metadata):
        self.end_started.set()
        if self.block_end:
            await self.allow_end.wait()
        if self.fail_end:
            self.active_stream = None
            raise RuntimeError("fake output drain failed")
        if self.active_stream and self.active_stream["stream_id"] == metadata["stream_id"]:
            self.active_stream = None

    async def abort_voice_stream(self, metadata):
        self.aborted_streams.append(metadata["stream_id"])
        self.active_stream = None

    async def stop_current(self):
        self.stop_current_count += 1

    async def interrupt(self):
        self.interrupt_count += 1
        self.active_stream = None
        for path in self.buffered_replies:
            Path(path).unlink(missing_ok=True)
        self.buffered_replies.clear()

    async def stop(self):
        self.stopped = True
        for path in self.buffered_replies:
            Path(path).unlink(missing_ok=True)
        self.buffered_replies.clear()
        self.active_stream = None


class ServicePlaybackContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        settings = load_service_settings(environ={"NACHOBOT_UNIVERSALVC_TOKEN": "playback-test-token"})
        vad_model = root / "vad.onnx"
        vad_model.write_bytes(b"placeholder used only to satisfy service path validation")
        self.settings = dataclasses.replace(
            settings,
            vad_model_path=vad_model,
            speaker_db_root=root / "speaker-db",
        )
        self.output: FakeOutput | None = None
        self.engine: UniversalVCSessionEngine | None = None

    def _make_engine(self):
        self.output = None

        def output_factory(config, logger, **kwargs):
            self.output = FakeOutput(config, logger, **kwargs)
            return self.output

        options = {
            "platform": "universal_vc",
            "scope": "playback-contract",
            "mode": "pipeline",
            "sources": [
                {
                    "id": source_id,
                    "input": "external",
                    "format": "f32le",
                    "sample_rate": 48_000,
                    "channels": 2,
                    "capture": {},
                    "identity": {"mode": "fixed", "id": f"identity-{source_id}", "name": source_id},
                }
                for source_id in ("main", "secondary")
            ],
            "output": {"mode": "device", "device_name": "Fake Output", "sample_rate": 48_000},
            "denoise": False,
            "speaker": False,
            "speaker_log": False,
            "max_duration_seconds": 10,
        }
        self.engine = UniversalVCSessionEngine(
            self.settings,
            options,
            service_instance_id="playback-contract-test",
            session_id="playback-session",
            session_generation="generation-1",
            pipeline_factory=FakePipeline,
            output_factory=output_factory,
            core_factory=lambda *_args: FakeCore(),
        )
        return self.engine

    async def asyncTearDown(self):
        if self.engine is not None:
            await self.engine.close()

    @staticmethod
    def _start(stream_id):
        return {
            "event": "start",
            "stream_id": stream_id,
            "sample_rate": 24_000,
            "channels": 1,
            "sample_width": 2,
            "codec": "pcm_s16le",
            "parent_message_id": f"reply-{stream_id}",
        }

    @staticmethod
    def _wav_bytes():
        stream = io.BytesIO()
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16_000)
            wav.writeframes(b"\x00\x00")
        return stream.getvalue()

    async def test_replacement_binds_one_stream_to_one_source_and_errors_retire_metadata(self):
        engine = self._make_engine()
        await engine.start()
        self.assertIsNotNone(self.output)
        output = self.output

        await engine.play_voice_stream("main", self._start("old-stream"), b"")
        await engine.play_voice_stream("secondary", self._start("new-stream"), b"")
        self.assertEqual(output.replaced_streams, ["old-stream"])
        self.assertEqual(set(engine._stream_playback_metadata), {"new-stream"})
        self.assertEqual(engine._active_stream_source, "secondary")

        with self.assertRaises(SessionRequestError):
            await engine.play_voice_stream(
                "main", {"event": "chunk", "stream_id": "new-stream"}, b"\x00\x00"
            )
        with self.assertRaises(SessionRequestError):
            await engine.play_voice_stream(
                "secondary", {"event": "chunk", "stream_id": "old-stream"}, b"\x00\x00"
            )
        self.assertEqual(output.chunks, [])

        await engine.play_voice_stream(
            "secondary", {"event": "chunk", "stream_id": "new-stream"}, b"\x00\x00"
        )
        self.assertEqual(len(output.chunks), 1)

        output.fail_chunk = True
        with self.assertRaisesRegex(RuntimeError, "chunk write failed"):
            await engine.play_voice_stream(
                "secondary", {"event": "chunk", "stream_id": "new-stream"}, b"\x00\x00"
            )
        self.assertEqual(engine._stream_playback_metadata, {})
        self.assertIsNone(engine._active_stream_source)
        self.assertEqual(output.aborted_streams, ["new-stream"])

        output.fail_chunk = False
        await engine.play_voice_stream("main", self._start("end-error-stream"), b"")
        output.fail_end = True
        with self.assertRaisesRegex(RuntimeError, "drain failed"):
            await engine.play_voice_stream(
                "main", {"event": "end", "stream_id": "end-error-stream"}, b""
            )
        self.assertEqual(engine._stream_playback_metadata, {})
        self.assertIsNone(engine._active_stream_source)
        with self.assertRaises(SessionRequestError):
            await engine.play_voice_stream(
                "main", {"event": "chunk", "stream_id": "end-error-stream"}, b"\x00\x00"
            )

    async def test_interrupt_discards_buffered_reply_and_turns_concurrent_drain_into_abort(self):
        engine = self._make_engine()
        await engine.start()
        output = self.output
        await engine.play_voice("main", self._wav_bytes())
        self.assertEqual(len(output.buffered_replies), 1)
        buffered_path = Path(output.buffered_replies[0])
        self.assertTrue(buffered_path.exists())

        await engine.play_voice_stream("main", self._start("racing-stream"), b"")
        output.block_end = True
        ending = asyncio.create_task(
            engine.play_voice_stream(
                "main", {"event": "end", "stream_id": "racing-stream"}, b""
            )
        )
        await asyncio.wait_for(output.end_started.wait(), timeout=1)

        await engine.interrupt()
        self.assertEqual(output.interrupt_count, 1)
        self.assertEqual(output.stop_current_count, 0)
        self.assertEqual(output.buffered_replies, [])
        self.assertFalse(buffered_path.exists())
        self.assertEqual(engine._stream_playback_metadata, {})
        self.assertIsNone(engine._active_stream_source)

        output.allow_end.set()
        result = await asyncio.wait_for(ending, timeout=1)
        self.assertEqual(result["status"], "aborted")

    async def test_replacement_during_end_drain_aborts_old_without_clearing_new_stream(self):
        engine = self._make_engine()
        await engine.start()
        output = self.output
        output.block_end = True
        await engine.play_voice_stream("main", self._start("stream-A"), b"")

        ending_a = asyncio.create_task(
            engine.play_voice_stream(
                "main", {"event": "end", "stream_id": "stream-A"}, b""
            )
        )
        await asyncio.wait_for(output.end_started.wait(), timeout=1)

        await engine.play_voice_stream("main", self._start("stream-B"), b"")
        self.assertEqual(set(engine._stream_playback_metadata), {"stream-B"})
        self.assertEqual(engine._active_stream_source, "main")

        output.allow_end.set()
        result_a = await asyncio.wait_for(ending_a, timeout=1)
        self.assertEqual(result_a["status"], "aborted")
        self.assertEqual(set(engine._stream_playback_metadata), {"stream-B"})
        self.assertEqual(engine._active_stream_source, "main")
        self.assertEqual(output.active_stream["stream_id"], "stream-B")

        await engine.play_voice_stream(
            "main", {"event": "chunk", "stream_id": "stream-B"}, b"\x00\x00"
        )
        result_b = await engine.play_voice_stream(
            "main", {"event": "end", "stream_id": "stream-B"}, b""
        )
        self.assertEqual(result_b["status"], "render_drained")
        self.assertEqual(engine._stream_playback_metadata, {})
        self.assertIsNone(engine._active_stream_source)

    async def test_legacy_stop_current_keeps_buffered_replies_but_interrupt_discards_them(self):
        output = AudioOutput(AudioOutputConfig(), logging.getLogger("playback-contract-test"))
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "queued.wav"
            wav_path.write_bytes(b"placeholder")
            output._queue.append(str(wav_path))

            await output.stop_current()
            self.assertEqual(list(output._queue), [str(wav_path)])
            self.assertTrue(wav_path.exists())

            await output.interrupt()
            self.assertEqual(list(output._queue), [])
            self.assertFalse(wav_path.exists())


if __name__ == "__main__":
    unittest.main()
