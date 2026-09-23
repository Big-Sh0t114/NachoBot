from __future__ import annotations

import unittest

from src.multimodal.api import PerceptionBody, TTSBody, create_multimodal_router, register_multimodal_api
from pydantic import ValidationError

from src.multimodal.contracts import MAX_MEDIA_REQUEST_CHARS, PerceptionResult, TTSResult
from src.multimodal.router import CoreMultimodalRouter


class _FakeProvider:
    async def health(self):
        return {"ready": True, "operations": ["audio.transcribe.v1"]}

    async def tts_health(self):
        return {"status": "ok", "ready": True, "model_loaded": True}

    async def perceive(self, request):
        return PerceptionResult(request.operation, "recognized", "fake")

    async def synthesize_tts(self, text, **kwargs):
        return TTSResult(text=text, audio_base64="YQ==", provider="fake")


class _Server:
    def __init__(self):
        self.registration = None

    def register_router(self, router, *, prefix):
        self.registration = (router, prefix)


class MultimodalApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_perception_and_tts_endpoints_use_core_facade(self):
        provider = _FakeProvider()
        service = CoreMultimodalRouter(profile="lite", local=provider, remote=provider)
        endpoints = {route.path: route.endpoint for route in create_multimodal_router(service).routes}

        perception = await endpoints["/audio/transcribe/v1"](
            PerceptionBody(operation="ignored", data="YQ==")
        )
        tts = await endpoints["/tts"](TTSBody(text="hello"))

        self.assertEqual(perception["operation"], "audio.transcribe.v1")
        self.assertEqual(perception["text"], "recognized")
        self.assertEqual(tts["audio_base64"], "YQ==")
        self.assertFalse(tts["text_only"])

    async def test_registers_under_authenticated_api_prefix(self):
        server = _Server()

        register_multimodal_api(server, CoreMultimodalRouter(profile="potato"))

        self.assertIsNotNone(server.registration)
        self.assertEqual(server.registration[1], "/api/multimodal")

    async def test_lite_health_uses_tts_service_without_local_perception(self):
        class LiteProvider(_FakeProvider):
            async def health(self):
                raise AssertionError("lite must not probe 9874")

        provider = LiteProvider()
        service = CoreMultimodalRouter(profile="lite", local=provider, remote=provider)
        endpoints = {route.path: route.endpoint for route in create_multimodal_router(service).routes}

        health = await endpoints["/health"]()

        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["desired_profile"], "lite")
        self.assertTrue(health["observed_local"]["tts"]["ready"])

    async def test_request_model_rejects_unbounded_media_strings(self):
        with self.assertRaises(ValidationError):
            PerceptionBody(operation="audio.transcribe.v1", data="A" * (MAX_MEDIA_REQUEST_CHARS + 1))


if __name__ == "__main__":
    unittest.main()
