import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import TTSPipeline, WebUITTSRequest


class _FixedModel:
    _initialized = True
    emotion_ready = True

    def __init__(self):
        self.calls = []

    async def tts(self, **kwargs):
        self.calls.append(kwargs)
        return b"RIFF-test"


class _StreamModel(_FixedModel):
    def __init__(self):
        super().__init__()
        self.stream_calls = []

    async def tts_stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        yield b"\x01\x02"
        yield b"\x03\x04"


class FixedPublicRuntimeTests(unittest.TestCase):
    def _pipeline(self, model=None):
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".toml",
            encoding="utf-8",
            delete=False,
        ) as config_file:
            config_file.write(
                "[server]\nhost = '127.0.0.1'\nport = 9880\n"
                "[enabled_tts]\nenabled = []\n"
                "[tts_base_config]\nstream_mode = false\npost_process = false\n"
            )
            config_path = Path(config_file.name)
        try:
            return TTSPipeline(
                config_path,
                backend="Vox",
                model=model or _FixedModel(),
            )
        finally:
            config_path.unlink(missing_ok=True)

    def test_public_routes_are_uniform_and_have_no_emotion_endpoint(self):
        pipeline = self._pipeline()
        routes = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in pipeline.app.routes
            if hasattr(route, "methods")
        }
        self.assertIn(("/api/tts", ("POST",)), routes)
        self.assertIn(("/api/tts-stream", ("POST",)), routes)
        self.assertIn(("/api/health", ("GET",)), routes)
        self.assertNotIn(("/api/emotion_preset", ("GET",)), routes)

    def test_stream_route_forwards_fixed_voice_and_split_method(self):
        async def scenario():
            model = _StreamModel()
            pipeline = self._pipeline(model)
            endpoint = next(
                route.endpoint for route in pipeline.app.routes if route.path == "/api/tts-stream"
            )
            response = await endpoint(
                WebUITTSRequest(
                    text="hello",
                    platform="local.host.cute",
                    text_lang="auto",
                    split_method="cut0",
                    voice_preset="default",
                )
            )
            chunks = [chunk async for chunk in response.body_iterator]
            self.assertEqual(b"\x01\x02\x03\x04", b"".join(chunks))
            self.assertEqual(
                model.stream_calls,
                [
                    {
                        "text": "hello",
                        "platform": "local.host.cute",
                        "text_lang": "auto",
                        "preset_name": "default",
                        "split_method": "cut0",
                    }
                ],
            )

        asyncio.run(scenario())

    def test_config_edits_do_not_hot_switch_fixed_model(self):
        async def scenario():
            model = _FixedModel()
            pipeline = self._pipeline(model)
            endpoint = next(route.endpoint for route in pipeline.app.routes if route.path == "/api/tts")
            with tempfile.TemporaryDirectory() as temp_dir:
                config = Path(temp_dir) / "base.toml"
                config.write_text('[enabled_tts]\nenabled=["GPT_Sovits"]\n', encoding="utf-8")
                first = await endpoint(WebUITTSRequest(text="one", platform="webui"))
                config.write_text('[enabled_tts]\nenabled=["Vox"]\n', encoding="utf-8")
                second = await endpoint(WebUITTSRequest(text="two", platform="webui"))
            self.assertEqual(first.body, b"RIFF-test")
            self.assertEqual(second.body, b"RIFF-test")
            self.assertEqual(len(model.calls), 2)

        asyncio.run(scenario())

    def test_child_death_makes_health_unhealthy_and_tts_503(self):
        async def scenario():
            pipeline = self._pipeline()
            self.assertTrue(pipeline._health_payload()["ready"])
            pipeline.set_backend_alive(False)
            health = pipeline._health_payload()
            self.assertFalse(health["ready"])
            self.assertNotEqual(health["status"], "ok")
            endpoint = next(route.endpoint for route in pipeline.app.routes if route.path == "/api/tts")
            with self.assertRaises(HTTPException) as caught:
                await endpoint(WebUITTSRequest(text="dead", platform="webui"))
            self.assertEqual(caught.exception.status_code, 503)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
