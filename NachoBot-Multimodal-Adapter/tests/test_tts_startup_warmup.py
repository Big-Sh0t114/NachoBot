import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import TTSPipeline, WebUITTSRequest
from fastapi import HTTPException
from nachobot_multimodal.tts.base import PCMChunk, EmptyPCMStreamError
from nachobot_multimodal.tts.backends.Vox.tts_model import TTSModel


class StartupWarmupTests(unittest.TestCase):
    def model(self, *, emotion=True, empty=False):
        model = TTSModel.__new__(TTSModel)
        model.config = SimpleNamespace(
            pipeline=SimpleNamespace(default_preset="default", platform_presets={"qq": "happy"}),
            emotion=SimpleNamespace(enabled=emotion, default_emotion="default",
                                    label_preset_map={"joy": "happy", "sadness": "sad", "calm": "same-default"}),
        )
        model._initialized = True
        model._emotion_classifier = None
        observed = []
        resolved = []
        closed = []

        def resolve(text, kwargs):
            name = kwargs["preset_name"]
            resolved.append(name)
            return {"text": text, "conditioning": "default" if name == "same-default" else name}, name

        async def stream(params, *, timeout_seconds):
            observed.append((params["conditioning"], timeout_seconds))
            try:
                if not empty:
                    yield PCMChunk(b"\x01\x00\x02\x00", sample_rate=48000)
            finally:
                closed.append(params["conditioning"])

        model._resolve_stream_request = resolve
        model._request_pcm_stream = stream
        return model, observed, resolved, closed

    def test_warms_reachable_conditioning_once_and_keeps_normal_deadline(self):
        async def scenario():
            model, observed, resolved, closed = self.model()
            await model.startup_warmup()
            self.assertEqual(observed, [("default", 900), ("happy", 900), ("sad", 900)])
            self.assertEqual(resolved, ["default", "happy", "sad", "same-default"])
            self.assertEqual(closed, ["default", "happy", "sad"])
            async for _ in model.tts_pcm_stream("normal", platform="qq", preset_name="happy"):
                pass
            self.assertEqual(observed[-1], ("happy", 120))
        asyncio.run(scenario())

    def test_disabled_emotion_does_not_warm_unrouted_presets(self):
        model, observed, _, _ = self.model(emotion=False)
        asyncio.run(model.startup_warmup())
        self.assertEqual(observed, [("default", 900), ("happy", 900)])

    def test_public_readiness_waits_and_empty_audio_fails_startup(self):
        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()
            async def warmup():
                started.set()
                await release.wait()
            model = SimpleNamespace(_initialized=True, emotion_ready=True, startup_warmup=warmup)
            pipeline = TTSPipeline(Path(__file__).resolve().parents[1] / "configs/base.toml", backend="Vox", model=model)
            lifecycle = pipeline._lifespan(pipeline.app)
            entering = asyncio.create_task(lifecycle.__aenter__())
            await started.wait()
            self.assertFalse(pipeline.ready)
            endpoint = next(route.endpoint for route in pipeline.app.routes if route.path == "/api/tts")
            with self.assertRaises(HTTPException) as caught:
                await endpoint(WebUITTSRequest(text="hello"))
            self.assertEqual(caught.exception.status_code, 503)
            release.set()
            await entering
            self.assertTrue(pipeline.ready)
            await lifecycle.__aexit__(None, None, None)
            self.assertFalse(pipeline.ready)

            empty_model, _, _, closed = self.model(empty=True)
            failed = TTSPipeline(Path(__file__).resolve().parents[1] / "configs/base.toml", backend="Vox", model=empty_model)
            with self.assertRaises(EmptyPCMStreamError):
                async with failed._lifespan(failed.app):
                    self.fail("invalid warmup must not publish readiness")
            self.assertFalse(failed.ready)
            self.assertEqual(closed, ["default"])
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
