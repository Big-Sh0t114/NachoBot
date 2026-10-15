"""Engine contract tests using injected fakes instead of native audio/model code."""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from universalvc_service.engine import (
    SessionRequestError,
    UniversalVCSessionEngine,
    validate_session_options,
)
from universalvc_service.service_config import load_service_settings


class FakePipeline:
    def __init__(self, config, logger, **callbacks):
        self.config = config
        self.callbacks = callbacks
        self.vad = SimpleNamespace(available=True)
        self.denoiser = SimpleNamespace(enabled=config.denoise.enabled)
        self.speaker_tracker = SimpleNamespace(enabled=config.speaker.enabled)
        self.loop = None
        self.interrupt_count = 0
        self.stopped = False

    def set_loop(self, loop):
        self.loop = loop

    def process_frame(self, _samples, _sample_rate, _channels):
        pass

    def process_mic_frame(self, _samples, _sample_rate, _channels):
        pass

    async def interrupt(self):
        self.interrupt_count += 1

    async def stop_streaming(self):
        self.stopped = True


class FakeCapture:
    def __init__(self, config, logger, **kwargs):
        self.config = config
        self.kwargs = kwargs
        self.started = False
        self.stopped = False

    async def start(self, _loop):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeOutput:
    def __init__(self, config, logger, **kwargs):
        self.config = config
        self.kwargs = kwargs
        self.initialized = False
        self.stopped = False

    def initialize(self):
        self.initialized = True

    async def stop(self):
        self.stopped = True


class FakeCore:
    def __init__(self, host, port, token):
        self.host, self.port, self.token = host, port, token
        self.closed = False

    async def close(self):
        self.closed = True


class ScriptedVAD:
    available = True

    def __init__(self):
        self.is_speaking = True
        self.frame_samples = []
        self.reset_count = 0

    def feed(self, samples):
        self.frame_samples.append(len(samples))
        return []

    def reset(self):
        self.reset_count += 1
        self.is_speaking = False


class ServiceEngineContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        settings = load_service_settings(environ={"NACHOBOT_UNIVERSALVC_TOKEN": "test-token"})
        vad_model = self.root / "vad.onnx"
        speaker_model = self.root / "speaker.onnx"
        vad_model.write_bytes(b"test placeholder; never loaded by the injected fake")
        speaker_model.write_bytes(b"test placeholder; never loaded by the injected fake")
        self.settings = dataclasses.replace(
            settings,
            vad_model_path=vad_model,
            speaker_model_path=speaker_model,
            speaker_db_root=self.root / "speaker-db",
            max_utterance_seconds=8.0,
        )

    @staticmethod
    def _process_source(source_id="main", *, speaker=True, target="VoiceApp.exe"):
        return {
            "id": source_id,
            "input": "process",
            "format": "f32le",
            "sample_rate": 48_000,
            "channels": 2,
            "capture": {"target_process_name": target},
            "identity": {"mode": "speaker"} if speaker else {
                "mode": "fixed", "id": "app-user", "name": "App User",
            },
        }

    @staticmethod
    def _mic_source(source_id="mic"):
        return {
            "id": source_id,
            "input": "microphone",
            "format": "f32le",
            "sample_rate": 48_000,
            "channels": 2,
            "capture": {
                "device_name": "USB Microphone",
                "push_to_talk": True,
                "ptt_key": "v",
            },
            "identity": {"mode": "fixed", "id": "owner", "name": "Owner"},
        }

    @staticmethod
    def _options(sources=None, **overrides):
        options = {
            "platform": "universal_vc",
            "scope": "engine-contract-test",
            "mode": "pipeline",
            "sources": sources or [ServiceEngineContractTests._process_source()],
            "output": {"mode": "device", "device_name": "Virtual Cable", "sample_rate": 48_000},
            "denoise": True,
            "speaker": True,
            "speaker_log": True,
            "max_duration_seconds": 5,
        }
        options.update(overrides)
        return options

    def _engine(self, options, *, session_id="session-a", events=None):
        created = {"pipelines": [], "captures": [], "microphones": [], "outputs": [], "cores": []}

        def make_pipeline(config, logger, **callbacks):
            pipeline = FakePipeline(config, logger, **callbacks)
            created["pipelines"].append(pipeline)
            return pipeline

        def make_capture(config, logger, **kwargs):
            capture = FakeCapture(config, logger, **kwargs)
            created["captures"].append(capture)
            return capture

        def make_microphone(config, logger, **kwargs):
            capture = FakeCapture(config, logger, **kwargs)
            created["microphones"].append(capture)
            return capture

        def make_output(config, logger, **kwargs):
            output = FakeOutput(config, logger, **kwargs)
            created["outputs"].append(output)
            return output

        def make_core(host, port, token):
            core = FakeCore(host, port, token)
            created["cores"].append(core)
            return core

        engine = UniversalVCSessionEngine(
            self.settings,
            options,
            service_instance_id="engine-contract-test",
            session_id=session_id,
            session_generation=f"generation-{session_id}",
            emit=(lambda event, source_id, **fields: events.append({
                "event": event, "source_id": source_id, **fields,
            })) if events is not None else None,
            pipeline_factory=make_pipeline,
            capture_factory=make_capture,
            microphone_factory=make_microphone,
            output_factory=make_output,
            core_factory=make_core,
        )
        return engine, created

    async def test_pipeline_sources_keep_callbacks_identity_flags_and_resources_isolated(self):
        options = self._options([self._process_source(), self._mic_source()])
        events = []
        first, first_created = self._engine(options, session_id="session-one", events=events)
        second, second_created = self._engine(options, session_id="session-two")
        try:
            with (
                patch("universalvc_service.denoise._RNNOISE_AVAILABLE", True),
                patch("universalvc_service.speaker_tracker._SHERPA_AVAILABLE", True),
                patch("universalvc_service.config.load_config", side_effect=AssertionError("standalone config must not load")) as load_config,
            ):
                await first.start()
                await second.start()
            load_config.assert_not_called()

            self.assertEqual(len(first_created["pipelines"]), 2)
            main, mic = first_created["pipelines"]
            self.assertEqual(main.config.capture.target_process_name, "VoiceApp.exe")
            self.assertFalse(main.config.capture.system_capture_device)
            self.assertTrue(main.config.denoise.enabled)
            self.assertTrue(main.config.speaker.enabled)
            self.assertEqual(mic.config.microphone.device_name, "USB Microphone")
            self.assertTrue(mic.config.microphone.push_to_talk)
            self.assertFalse(mic.config.speaker.enabled)
            self.assertEqual(main.callbacks["speaker_log"], True)
            self.assertEqual(mic.callbacks["speaker_log"], True)
            self.assertEqual(main.MAX_UTTERANCE_SECONDS, 5)
            self.assertEqual(mic.MAX_UTTERANCE_SECONDS, 5)

            self.assertEqual(len(first_created["captures"]), 1)
            self.assertEqual(first_created["captures"][0].config.target_process_name, "VoiceApp.exe")
            self.assertEqual(len(first_created["microphones"]), 1)
            self.assertEqual(first_created["microphones"][0].config.device_name, "USB Microphone")
            self.assertEqual(first_created["outputs"][0].config.device_name, "Virtual Cable")
            self.assertTrue(first_created["outputs"][0].initialized)

            main_db = Path(main.config.speaker.db_path).resolve()
            second_main_db = Path(second_created["pipelines"][0].config.speaker.db_path).resolve()
            self.assertEqual(main_db.parent, (self.settings.speaker_db_root / "session-one").resolve())
            self.assertEqual(second_main_db.parent, (self.settings.speaker_db_root / "session-two").resolve())
            self.assertNotEqual(main_db, second_main_db)
            self.assertNotEqual(
                main_db,
                Path(mic.config.speaker.db_path).resolve(),
                "per-source speaker stores must be isolated within a session",
            )

            self.assertIsNotNone(main.callbacks.get("on_speech_start_with_scope"))
            self.assertIsNotNone(main.callbacks.get("on_speech_end_with_scope"))
            self.assertIsNone(main.callbacks.get("on_mic_speech_start_with_scope"))
            self.assertIsNone(main.callbacks.get("on_mic_speech_end_with_scope"))
            self.assertIsNotNone(mic.callbacks.get("on_mic_speech_start_with_scope"))
            self.assertIsNotNone(mic.callbacks.get("on_mic_speech_end_with_scope"))

            turn_ids = {}
            for source_id, pipeline, start_key, end_key in (
                ("main", main, "on_speech_start_with_scope", "on_speech_end_with_scope"),
                ("mic", mic, "on_mic_speech_start_with_scope", "on_mic_speech_end_with_scope"),
            ):
                callbacks = pipeline.callbacks
                scope = callbacks["stream_scope_factory"]("same-context-key")
                self.assertTrue(scope.startswith("uvc1:"))
                await callbacks[start_key](scope)
                await callbacks[end_key](scope)
                await callbacks["on_result_with_scope"](
                    f"speaker-{source_id}",
                    f"Speaker {source_id}",
                    "AA==",
                    f"result-{source_id}",
                    f"recognized {source_id}",
                    scope,
                )
                source_events = [item for item in events if item["source_id"] == source_id]
                self.assertEqual([item["event"] for item in source_events], ["speech_start", "speech_end", "speech_result"])
                self.assertEqual(len({item["turn_id"] for item in source_events}), 1)
                self.assertNotEqual(source_events[0]["turn_id"], scope)
                self.assertEqual(source_events[-1]["asr_scope"], scope)
                self.assertNotIn(scope, first._scope_turns, "completed receipt scopes must be retired")
                turn_ids[source_id] = source_events[0]["turn_id"]

            self.assertNotEqual(turn_ids["main"], turn_ids["mic"])
        finally:
            await first.close()
            await second.close()

    async def test_vad_duration_accumulates_across_frames_and_rejects_overlong_turn(self):
        options = self._options(
            [
                {
                    "id": "external",
                    "input": "external",
                    "format": "s16le",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "capture": {},
                    "identity": {"mode": "fixed", "id": "speaker", "name": "Speaker"},
                }
            ],
            mode="vad",
            denoise=False,
            speaker=False,
            speaker_log=True,
            max_duration_seconds=1,
            output={"mode": "none", "sample_rate": 48_000},
        )
        vad = ScriptedVAD()
        engine, _created = self._engine(options)
        engine.vad_factory = lambda **_kwargs: vad
        try:
            await engine.start()
            half_second = b"\x00\x00" * 8_000
            self.assertTrue((await engine.command("external", "feed_vad", {"seq": 0}, half_second))["speaking"])
            self.assertTrue((await engine.command("external", "feed_vad", {"seq": 1}, half_second))["speaking"])
            with self.assertRaisesRegex(SessionRequestError, "exceeded max_duration_seconds"):
                await engine.command("external", "feed_vad", {"seq": 2}, half_second)
            self.assertEqual(vad.frame_samples, [8_000, 8_000, 8_000])
            self.assertEqual(vad.reset_count, 1)
            self.assertFalse(engine.sources["external"].speaking)
        finally:
            await engine.close()

    async def test_source_id_cannot_escape_its_speaker_database_directory(self):
        malicious_id = "../../outside"
        options = self._options([self._process_source(malicious_id)])
        try:
            validate_session_options(options, self.settings)
        except SessionRequestError:
            return

        engine, created = self._engine(options, session_id="contained-session")
        try:
            with (
                patch("universalvc_service.denoise._RNNOISE_AVAILABLE", True),
                patch("universalvc_service.speaker_tracker._SHERPA_AVAILABLE", True),
            ):
                await engine.start()
            db_path = Path(created["pipelines"][0].config.speaker.db_path).resolve()
            expected_root = (self.settings.speaker_db_root / "contained-session").resolve()
            self.assertEqual(db_path.parent, expected_root)
        finally:
            await engine.close()


if __name__ == "__main__":
    unittest.main()
