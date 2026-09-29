from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from src.multimodal.api import PerceptionBody, TTSBody, create_multimodal_router, register_multimodal_api
from pydantic import ValidationError

from src.multimodal.contracts import MAX_MEDIA_REQUEST_CHARS, PerceptionResult, TTSResult
from src.multimodal.router import AudioStreamError, CoreMultimodalRouter


class _FakeProvider:
    async def health(self):
        return {"ready": True, "operations": ["audio.transcribe.v1"], "models": {"audio.transcribe.v1": "zh-xlarge-int8-2025-06-30"}}

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


class _StreamService:
    def __init__(self):
        self.started_platforms = []

    async def start_audio_stream(self, *, sample_rate, channels, platform="universal_vc"):
        if sample_rate != 16_000 or channels != 1:
            raise AudioStreamError(400, "invalid_audio_format")
        if platform not in {"universal_vc", "discord_vc", "bilibili", "webui"}:
            raise AudioStreamError(400, "invalid_platform")
        self.started_platforms.append(platform)
        return {"stream_id": "core-stream"}

    async def append_audio_stream_chunk(self, *, stream_id, seq, pcm_base64):
        if seq != 0:
            raise AudioStreamError(409, "sequence_conflict")
        return {"seq": seq, "partial_text": "partial"}

    async def finish_audio_stream(self, stream_id):
        return {"text": "done", "result_id": "receipt"}

    async def abort_audio_stream(self, stream_id):
        return {"aborted": True}


class MultimodalApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_perception_and_tts_endpoints_use_core_facade(self):
        from src.llm_models.utils_model import LLMRequest

        provider = _FakeProvider()
        model = SimpleNamespace(name="local", api_provider="local", model_identifier="zh-xlarge-int8-2025-06-30")
        api_provider = SimpleNamespace(base_url="http://127.0.0.1:9874/v1")
        config = SimpleNamespace(
            model_task_config=SimpleNamespace(voice=SimpleNamespace(model_list=["local"])),
            get_model_info=lambda name: model,
            get_provider=lambda name: api_provider,
        )
        service = CoreMultimodalRouter(profile="full", local=provider, model_config=config)
        endpoints = {route.path: route.endpoint for route in create_multimodal_router(service).routes}

        with patch.object(LLMRequest, "_select_model", return_value=(model, api_provider, object())):
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

    async def test_emoji_jpeg_keeps_visual_group_and_fast_image_uses_fast_group(self):
        service = CoreMultimodalRouter(profile="lite")
        seen = []

        async def perceive(request):
            seen.append((request.task, request.media_format))
            return PerceptionResult(request.operation, "ok", "fake")

        service.perceive = perceive
        endpoints = {route.path: route.endpoint for route in create_multimodal_router(service).routes}
        await endpoints["/image/emoji/v1"](PerceptionBody(operation="ignored", data="YQ==", media_format="jpeg"))
        await endpoints["/image/describe/fast"](PerceptionBody(operation="ignored", data="YQ==", media_format="jpeg"))

        self.assertEqual(seen, [("vlm", "jpeg"), ("vlm_fast", "jpeg")])

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

    async def test_audio_stream_http_contract_and_error_statuses(self):
        app = FastAPI()
        service = _StreamService()
        app.include_router(create_multimodal_router(service), prefix="/api/multimodal")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = await client.post(
                "/api/multimodal/audio/stream/start",
                json={"sample_rate": 16_000, "channels": 1},
            )
            discord_started = await client.post(
                "/api/multimodal/audio/stream/start",
                json={"sample_rate": 16_000, "channels": 1, "platform": "discord_vc"},
            )
            invalid_platform = await client.post(
                "/api/multimodal/audio/stream/start",
                json={"sample_rate": 16_000, "channels": 1, "platform": "untrusted"},
            )
            invalid = await client.post(
                "/api/multimodal/audio/stream/start",
                json={"sample_rate": 8_000, "channels": 1},
            )
            malformed = await client.post(
                "/api/multimodal/audio/stream/start",
                content="[",
                headers={"content-type": "application/json"},
            )
            oversized = await client.post(
                "/api/multimodal/audio/stream/chunk",
                content=(
                    b'{"stream_id":"core-stream","seq":0,"pcm_base64":"'
                    + b"A" * 90_001
                    + b'"}'
                ),
                headers={"content-type": "application/json"},
            )
            chunk = await client.post(
                "/api/multimodal/audio/stream/chunk",
                json={"stream_id": "core-stream", "seq": 0, "pcm_base64": "AA=="},
            )
            gap = await client.post(
                "/api/multimodal/audio/stream/chunk",
                json={"stream_id": "core-stream", "seq": 1, "pcm_base64": "AA=="},
            )
            finished = await client.post(
                "/api/multimodal/audio/stream/finish",
                json={"stream_id": "core-stream"},
            )
            aborted = await client.post(
                "/api/multimodal/audio/stream/abort",
                json={"stream_id": "core-stream"},
            )

        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json(), {"stream_id": "core-stream"})
        self.assertEqual(discord_started.status_code, 200)
        self.assertEqual(service.started_platforms, ["universal_vc", "discord_vc"])
        self.assertEqual(invalid_platform.status_code, 400)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(chunk.json(), {"seq": 0, "partial_text": "partial"})
        self.assertEqual(gap.status_code, 409)
        self.assertEqual(finished.json(), {"text": "done", "result_id": "receipt"})
        self.assertEqual(aborted.json(), {"aborted": True})


if __name__ == "__main__":
    unittest.main()
