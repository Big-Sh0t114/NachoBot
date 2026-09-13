import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import types
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nachobot_multimodal.utils.tts_resolver import TTSResolution
from nachobot_multimodal.utils.tts_runtime import TTSRuntime


class _RecordingModel:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def tts(self, **kwargs):
        self.calls.append(kwargs)
        return self.name.encode("ascii")

    def tts_stream(self, **kwargs):
        self.calls.append(kwargs)
        return _RecordingStream(self.name.encode("ascii"))


class _EmotionModel(_RecordingModel):
    def __init__(self, name, enabled=True):
        super().__init__(name)
        self.config = SimpleNamespace(
            emotion=SimpleNamespace(
                enabled=enabled,
                classifier_model="fake",
                classifier_device="cpu",
                use_fp16=False,
                confidence_threshold=0.4,
                default_emotion="default",
                label_preset_map={},
            )
        )


class _RecordingStream:
    def __init__(self, chunk):
        self.chunk = chunk
        self.sent = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return self.chunk

    async def aclose(self):
        self.closed = True


class _Runtime:
    def __init__(self, model):
        self.model = model
        self.fingerprint = model.name
        self.entries = 0
        self.closed_streams = []
        self.events = []

    @property
    def ready(self):
        return self.model is not None

    @property
    def error(self):
        return None

    def model_context(self):
        runtime = self

        class Context:
            async def __aenter__(self):
                runtime.entries += 1
                return runtime.model

            async def __aexit__(self, exc_type, exc, tb):
                runtime.events.append("exit")
                return False

        return Context()

    async def close_stream(self, stream):
        self.closed_streams.append(stream)
        await stream.aclose()
        self.events.append("closed")


class _HealthRuntime:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.index = 0
        self._model = None

    @property
    def model(self):
        return self._model

    def status(self):
        return {
            "ready": self._model is not None,
            "backend": type(self._model).__module__ if self._model else None,
            "fingerprint": None,
            "error": None,
        }

    def desired_status(self):
        snapshot = self.snapshots[self.index]
        return {
            "backend": snapshot.model_class.__module__ if snapshot.model_class and not snapshot.error else None,
            "plugin": snapshot.plugin,
            "fingerprint": snapshot.fingerprint,
            "error": snapshot.error,
        }


class CallerHotReloadTests(unittest.TestCase):
    def test_pipeline_refreshes_each_nonstream_boundary_and_sets_skip_remote_emotion(self):
        async def scenario():
            from main import TTSPipeline

            first = _RecordingModel("vox")
            second = _RecordingModel("gpt")
            runtime = _Runtime(first)
            pipeline = TTSPipeline.__new__(TTSPipeline)
            pipeline.no_local_models = False
            pipeline._tts_runtime = runtime
            pipeline._emotion_classifier = None
            pipeline._emotion_config = None
            pipeline._emotion_model_fingerprint = None
            pipeline.tts_list = []
            pipeline.config = SimpleNamespace(
                tts_base_config=SimpleNamespace(post_process=False)
            )

            first_result = await pipeline.get_voice_no_stream("one", "webui")
            runtime.model = second
            runtime.fingerprint = second.name
            second_result = await pipeline.get_voice_no_stream("two", "webui")

            self.assertIsNotNone(first_result)
            self.assertIsNotNone(second_result)
            self.assertEqual(runtime.entries, 2)
            self.assertIs(pipeline.tts_list[0], second)
            self.assertTrue(first.calls[0]["skip_remote_emotion"])
            self.assertTrue(second.calls[0]["skip_remote_emotion"])

        asyncio.run(scenario())

    def test_webui_http_endpoint_refreshes_and_suppresses_remote_emotion_retry(self):
        async def scenario():
            from fastapi import FastAPI
            from main import TTSPipeline, WebUITTSRequest

            first = _RecordingModel("vox")
            second = _RecordingModel("gpt")
            runtime = _Runtime(first)
            pipeline = TTSPipeline.__new__(TTSPipeline)
            pipeline.no_local_models = False
            pipeline._tts_runtime = runtime
            pipeline._emotion_classifier = None
            pipeline._emotion_config = None
            pipeline._emotion_model_fingerprint = None
            pipeline.tts_list = []
            pipeline.config = SimpleNamespace()
            pipeline._webui_tts_lock = asyncio.Lock()
            app = FastAPI()
            pipeline.server = SimpleNamespace(connection=SimpleNamespace(app=app))
            pipeline._register_http_endpoints()
            endpoint = next(route.endpoint for route in app.routes if route.path == "/api/tts")

            first_response = await endpoint(WebUITTSRequest(text="one", platform="webui"))
            runtime.model = second
            second_response = await endpoint(WebUITTSRequest(text="two", platform="webui"))

            self.assertEqual(first_response.body, b"vox")
            self.assertEqual(second_response.body, b"gpt")
            self.assertEqual(runtime.entries, 2)
            self.assertTrue(first.calls[0]["skip_remote_emotion"])
            self.assertTrue(second.calls[0]["skip_remote_emotion"])

        asyncio.run(scenario())

    def test_pipeline_stream_closes_before_context_exit(self):
        async def scenario():
            from main import TTSPipeline

            model = _RecordingModel("stream")
            runtime = _Runtime(model)
            pipeline = TTSPipeline.__new__(TTSPipeline)
            pipeline.no_local_models = False
            pipeline._tts_runtime = runtime
            pipeline._emotion_classifier = None
            pipeline._emotion_config = None
            pipeline._emotion_model_fingerprint = None
            pipeline.tts_list = []
            pipeline.config = SimpleNamespace()

            sent = []

            class Server:
                async def send_message(self, message):
                    sent.append(message)
                    return True

            pipeline.server = Server()
            message = SimpleNamespace(
                message_segment=SimpleNamespace(type="tts_text", data="stream text"),
                message_info=SimpleNamespace(
                    platform="webui",
                    additional_config={},
                    format_info=SimpleNamespace(content_format=[]),
                ),
            )
            await pipeline.send_voice_stream(message)

            self.assertEqual(len(sent), 1)
            self.assertEqual(len(runtime.closed_streams), 1)
            stream = runtime.closed_streams[0]
            self.assertTrue(stream.closed)
            self.assertEqual(runtime.events, ["closed", "exit"])
            self.assertTrue(model.calls[0]["skip_remote_emotion"])

        asyncio.run(scenario())

    def test_health_advertises_repaired_selection_without_instantiating_model(self):
        from main import TTSPipeline

        class VoxMarker:
            pass

        class GptMarker:
            pass

        valid = TTSResolution(VoxMarker, None, "vox-fingerprint", "Vox", None, None)
        invalid = TTSResolution(None, "No TTS plugins enabled", None, None, None, None)
        runtime = _HealthRuntime([valid, invalid, valid])
        pipeline = TTSPipeline.__new__(TTSPipeline)
        pipeline.no_local_models = False
        pipeline._tts_runtime = runtime
        pipeline._emotion_classifier = None

        first = pipeline._health_payload()
        self.assertEqual(first["tts_backends"], [VoxMarker.__module__])
        self.assertFalse(first["tts_ready"])

        runtime.index = 1
        broken = pipeline._health_payload()
        self.assertEqual(broken["tts_backends"], [])
        self.assertIn("No TTS", broken["tts_error"])

        runtime.index = 2
        repaired = pipeline._health_payload()
        self.assertEqual(repaired["tts_backends"], [VoxMarker.__module__])
        self.assertFalse(repaired["tts_ready"])

    def test_classifier_resets_on_gpt_and_recreates_on_vox_return(self):
        async def scenario():
            from main import TTSPipeline

            first = _EmotionModel("vox")
            second = _RecordingModel("gpt")
            third = _EmotionModel("vox-again")
            runtime = _Runtime(first)
            pipeline = TTSPipeline.__new__(TTSPipeline)
            pipeline.no_local_models = False
            pipeline._tts_runtime = runtime
            pipeline._emotion_classifier = None
            pipeline._emotion_config = None
            pipeline._emotion_model_fingerprint = None
            pipeline.tts_list = []
            pipeline.config = SimpleNamespace(
                tts_base_config=SimpleNamespace(post_process=False)
            )
            created = []

            class FakeClassifier:
                def __init__(self, **kwargs):
                    created.append(self)

            fake_module = types.ModuleType("nachobot_multimodal.utils.emotion_classifier")
            fake_module.EmotionClassifier = FakeClassifier
            with mock.patch.dict(
                sys.modules,
                {"nachobot_multimodal.utils.emotion_classifier": fake_module},
            ):
                await pipeline.get_voice_no_stream("one", "webui")
                first_classifier = pipeline._emotion_classifier
                runtime.model = second
                runtime.fingerprint = second.name
                await pipeline.get_voice_no_stream("two", "webui")
                self.assertIsNone(pipeline._emotion_classifier)
                runtime.model = third
                runtime.fingerprint = third.name
                await pipeline.get_voice_no_stream("three", "webui")

            self.assertEqual(len(created), 2)
            self.assertIsNot(pipeline._emotion_classifier, first_classifier)
            self.assertIs(pipeline.tts_list[0], third)

        asyncio.run(scenario())

    def test_relay_only_pipeline_never_enters_tts_runtime(self):
        async def scenario():
            from main import TTSPipeline

            pipeline = TTSPipeline.__new__(TTSPipeline)
            pipeline.no_local_models = True
            pipeline._tts_runtime = None
            self.assertIsNone(await pipeline.get_voice_no_stream("relay", "webui"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
