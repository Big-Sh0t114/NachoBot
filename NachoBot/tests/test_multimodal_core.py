from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ncnk_message import Seg

from src.multimodal.contracts import (
    AUDIO_TRANSCRIBE_V1,
    IMAGE_DESCRIBE_V1,
    MAX_AUDIO_BYTES,
    MediaInput,
    PerceptionResult,
    TTSResult,
    decode_bounded_base64,
    max_base64_chars,
)
from src.multimodal.client import LocalMultimodalClient
from src.multimodal.remote import remote_task_config
from src.multimodal.profile import RuntimeProfile, get_runtime_profile
from src.multimodal.router import CoreMultimodalRouter
from src.chat.message_receive.message import MessageProcessBase
from src.chat.heart_flow.heartFC_chat import _prepare_json_envelope_delivery


class FakePerception:
    def __init__(self, result: PerceptionResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[str] = []
        self.tts_calls: list[str] = []
        self.tts_result: TTSResult | None = TTSResult(text="", audio_base64="YQ==", provider="fake")

    async def perceive(self, request: MediaInput) -> PerceptionResult:
        self.calls.append(request.operation)
        if self.error:
            raise self.error
        return self.result or PerceptionResult(request.operation, text="ok", provider="fake")

    async def synthesize_tts(self, text: str, **kwargs) -> TTSResult:
        self.tts_calls.append(text)
        if self.error:
            raise self.error
        return self.tts_result or TTSResult(text=text, provider="fake")

    async def health(self):
        return {"ready": True}

    async def tts_health(self):
        return {"status": "ok", "ready": True, "model_loaded": True}


class _BinaryResponse:
    content = b"RIFF-wav"
    headers = {"content-type": "audio/wav"}

    def raise_for_status(self):
        return None


class _RecordingHttpClient:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return _BinaryResponse()


class _JsonResponse:
    content = b""
    headers = {"content-type": "application/json"}

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _JsonHttpClient(_RecordingHttpClient):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    async def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return _JsonResponse(self.payload)


class MultimodalCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_outbound_materialized_voice_is_not_transcribed_again(self):
        voice_text = await MessageProcessBase._process_single_segment(
            object(),
            Seg(type="voice", data="YQ=="),
        )
        stream_text = await MessageProcessBase._process_single_segment(
            object(),
            Seg(type="voice_stream", data="YQ=="),
        )

        self.assertEqual(voice_text, "")
        self.assertEqual(stream_text, "")

    async def test_full_uses_local_then_remote_only_after_local_failure(self):
        local = FakePerception(error=RuntimeError("offline"))
        remote = FakePerception(PerceptionResult(AUDIO_TRANSCRIBE_V1, "remote", "remote"))
        router = CoreMultimodalRouter(profile="full", local=local, remote=remote)

        result = await router.transcribe("YQ==")

        self.assertEqual(result.text, "remote")
        self.assertEqual(local.calls, [AUDIO_TRANSCRIBE_V1])
        self.assertEqual(remote.calls, [AUDIO_TRANSCRIBE_V1])
        self.assertEqual(result.attempted, ("local", "remote"))

    async def test_lite_skips_local_and_remote_failure_degrades_once(self):
        local = FakePerception()
        remote = FakePerception(error=RuntimeError("offline"))
        router = CoreMultimodalRouter(profile="lite", local=local, remote=remote)

        result = await router.describe_image("YQ==", media_format="png")

        self.assertTrue(result.degraded)
        self.assertEqual(local.calls, [])
        self.assertEqual(remote.calls, [IMAGE_DESCRIBE_V1])
        self.assertIn("图片", result.text)

    async def test_no_media_does_not_trigger_tts_or_perception(self):
        local = FakePerception()
        router = CoreMultimodalRouter(profile="full", local=local, remote=FakePerception())
        plain = Seg(type="seglist", data=[Seg(type="text", data="hello")])

        self.assertFalse(router.has_explicit_tts_text(plain))
        materialized = await router.materialize_reply(plain)

        self.assertEqual(materialized.to_dict(), plain.to_dict())
        self.assertEqual(local.tts_calls, [])
        self.assertEqual(local.calls, [])

    async def test_tts_is_reply_field_driven_and_nested(self):
        local = FakePerception()
        router = CoreMultimodalRouter(profile="lite", local=local, remote=FakePerception())
        reply = Seg(
            type="seglist",
            data=[Seg(type="text", data="ordinary"), Seg(type="tts_text", data={"text": "say this"})],
        )

        materialized = await router.materialize_reply(reply)

        self.assertEqual(local.tts_calls, ["say this"])
        self.assertEqual(
            [segment.type for segment in materialized.data],  # type: ignore[union-attr]
            ["text", "tts_text", "voice"],
        )

    async def test_single_tts_field_keeps_text_and_adds_compatible_voice(self):
        local = FakePerception()
        router = CoreMultimodalRouter(profile="full", local=local, remote=FakePerception())

        materialized = await router.materialize_reply(Seg(type="tts_text", data="say"))

        self.assertEqual([segment.type for segment in materialized.data], ["tts_text", "voice"])  # type: ignore[union-attr]

    async def test_tts_failure_degrades_to_sendable_text(self):
        local = FakePerception(error=RuntimeError("tts unavailable"))
        router = CoreMultimodalRouter(profile="full", local=local, remote=FakePerception())
        original = Seg(type="tts_text", data={"text": "keep text"})

        materialized = await router.materialize_reply(original)

        self.assertEqual(materialized.to_dict(), {"type": "text", "data": "keep text"})

    async def test_tts_transport_text_is_used_for_failure_and_potato_fallback(self):
        original = Seg(
            type="tts_text",
            data={"text": "speak this", "display_text": '{"reply":"show this"}'},
        )
        failed = CoreMultimodalRouter(
            profile="full",
            local=FakePerception(error=RuntimeError("tts unavailable")),
            remote=FakePerception(),
        )
        potato = CoreMultimodalRouter(
            profile="potato",
            local=FakePerception(),
            remote=FakePerception(),
        )

        self.assertEqual(
            (await failed.materialize_reply(original)).to_dict(),
            {"type": "text", "data": '{"reply":"show this"}'},
        )
        self.assertEqual(
            (await potato.materialize_reply(original)).to_dict(),
            {"type": "text", "data": '{"reply":"show this"}'},
        )

    async def test_segment_language_overrides_outer_tts_language(self):
        local = FakePerception()
        router = CoreMultimodalRouter(profile="full", local=local, remote=FakePerception())
        captured = {}

        async def synthesize(text, **kwargs):
            captured.update(kwargs)
            return TTSResult(text=text, audio_base64="YQ==", provider="fake")

        local.synthesize_tts = synthesize
        await router.materialize_reply(
            Seg(type="tts_text", data={"text": "中文", "lang": "zh"}),
            text_lang="ja",
        )

        self.assertEqual(captured["text_lang"], "zh")

    def test_json_envelope_explicit_tts_field_is_preserved_as_sidecar(self):
        raw = '{"reply":"给观众看的中文","tts_text":"音声です","emotion":"normal"}'

        display, payload = _prepare_json_envelope_delivery(raw, tts_language="ja")

        self.assertEqual(display, "给观众看的中文")
        self.assertEqual(payload, {"text": "音声です", "display_text": raw, "lang": "ja"})

    def test_json_envelope_legacy_tags_are_converted_only_when_tts_enabled(self):
        raw = '{"reply":"<JP>音声です</JP><ZH>展示文本</ZH>","emotion":"normal"}'

        disabled_display, disabled_payload = _prepare_json_envelope_delivery(raw)
        display, payload = _prepare_json_envelope_delivery(raw, tts_language="ja")

        self.assertIsNone(disabled_payload)
        self.assertIn("<JP>", disabled_display)
        self.assertEqual(display, "展示文本")
        self.assertEqual(payload["text"], "音声です")
        self.assertIn('"reply": "展示文本"', payload["display_text"])

    async def test_core_tts_uses_9880_binary_audio_not_9874_perception(self):
        http_client = _RecordingHttpClient()
        local = LocalMultimodalClient(
            endpoint="http://127.0.0.1:9874",
            client=http_client,
        )
        router = CoreMultimodalRouter(profile="full", local=local, remote=FakePerception())

        result = await router.synthesize_tts("say this", platform="qq")

        self.assertEqual(result.audio_base64, "UklGRi13YXY=")
        self.assertEqual(result.audio_format, "wav")
        self.assertEqual([url for _, url, _ in http_client.calls], ["http://127.0.0.1:9880/api/tts"])
        self.assertEqual(http_client.calls[0][2]["json"]["platform"], "qq")

    async def test_health_probes_profile_required_components_only(self):
        full_local = FakePerception()
        full = await CoreMultimodalRouter(
            profile="full", local=full_local, remote=FakePerception()
        ).health()
        self.assertTrue(full["ready"])
        self.assertTrue(full["perception"]["required"])
        self.assertTrue(full["tts"]["required"])

        class LiteLocal(FakePerception):
            async def health(self):
                raise AssertionError("lite must not probe local perception")

        lite = await CoreMultimodalRouter(
            profile="lite", local=LiteLocal(), remote=FakePerception()
        ).health()
        self.assertTrue(lite["ready"])
        self.assertFalse(lite["perception"]["required"])
        self.assertTrue(lite["tts"]["required"])

        class PotatoLocal(FakePerception):
            async def health(self):
                raise AssertionError("potato must not probe local perception")

            async def tts_health(self):
                raise AssertionError("potato must not probe TTS")

        potato = await CoreMultimodalRouter(
            profile="potato", local=PotatoLocal(), remote=FakePerception()
        ).health()
        self.assertTrue(potato["ready"])
        self.assertFalse(potato["perception"]["required"])
        self.assertFalse(potato["tts"]["required"])

    async def test_tts_health_requires_loaded_public_9880_model(self):
        active_http = _JsonHttpClient(
            {"status": "ok", "ready": True, "model_loaded": True}
        )
        active = LocalMultimodalClient(
            client=active_http,
        )
        self.assertTrue((await active.tts_health())["ready"])
        self.assertEqual(
            active_http.calls[0][1],
            "http://127.0.0.1:9880/api/health",
        )

        unloaded_http = _JsonHttpClient(
            {"status": "ok", "ready": True, "model_loaded": False}
        )
        unloaded = LocalMultimodalClient(client=unloaded_http)
        self.assertFalse((await unloaded.tts_health())["ready"])

        relay_http = _JsonHttpClient(
            {"status": "ok", "mode": "tts", "tts_backends": ["Vox"]}
        )
        relay = LocalMultimodalClient(client=relay_http)
        self.assertFalse((await relay.tts_health())["ready"])

    async def test_potato_converts_tts_to_plain_text_without_calling_tts(self):
        local = FakePerception()
        router = CoreMultimodalRouter(profile="potato", local=local, remote=FakePerception())

        materialized = await router.materialize_reply(Seg(type="tts_text", data="plain"))

        self.assertEqual(materialized.to_dict(), {"type": "text", "data": "plain"})
        self.assertEqual(local.tts_calls, [])

    async def test_potato_converts_empty_legacy_tts_field_to_text(self):
        router = CoreMultimodalRouter(profile="potato", local=FakePerception(), remote=FakePerception())

        materialized = await router.materialize_reply(Seg(type="tts_text", data=""))

        self.assertEqual(materialized.to_dict(), {"type": "text", "data": ""})

    async def test_potato_preserves_prebuilt_media_and_converts_only_tts_text(self):
        router = CoreMultimodalRouter(profile="potato", local=FakePerception(), remote=FakePerception())
        reply = Seg(
            type="seglist",
            data=[
                Seg(type="voice", data="YQ=="),
                Seg(type="text", data="plain"),
                Seg(type="tts_text", data="speak this"),
                Seg(type="voice_stream", data="Yg=="),
                Seg(type="music", data={"url": "https://example.invalid/track"}),
                Seg(type="audio", data=b"prebuilt-audio"),
            ],
        )

        self.assertTrue(router.should_materialize_reply(reply))
        materialized = await router.materialize_reply(reply)

        self.assertEqual(materialized.to_dict(), {
            "type": "seglist",
            "data": [
                {"type": "voice", "data": "YQ=="},
                {"type": "text", "data": "plain"},
                {"type": "text", "data": "speak this"},
                {"type": "voice_stream", "data": "Yg=="},
                {"type": "music", "data": {"url": "https://example.invalid/track"}},
                {"type": "audio", "data": b"prebuilt-audio"},
            ],
        })
        self.assertEqual(router.local.tts_calls, [])


class RemoteTaskFilteringTests(unittest.TestCase):
    def test_excludes_local_9874_provider_from_mixed_task(self):
        local = SimpleNamespace(name="LocalModel", base_url="http://127.0.0.1:9874/v1")
        remote = SimpleNamespace(name="Remote", base_url="https://example.invalid/v1")
        model_config = SimpleNamespace(
            api_providers=[local, remote],
            models=[
                SimpleNamespace(name="local", api_provider="LocalModel"),
                SimpleNamespace(name="remote", api_provider="Remote"),
            ],
        )
        model_config.get_model_info = lambda name: next(model for model in model_config.models if model.name == name)
        task = SimpleNamespace(model_list=["local", "remote"], max_tokens=10)

        filtered = remote_task_config(task, model_config)

        self.assertEqual(filtered.model_list, ["remote"])

    def test_unknown_model_names_are_not_classified_by_core(self):
        task = SimpleNamespace(model_list=["adapter-owned-model"])
        model_config = SimpleNamespace(api_providers=[], models=[])
        model_config.get_model_info = lambda name: (_ for _ in ()).throw(KeyError(name))

        filtered = remote_task_config(task, model_config)

        self.assertEqual(filtered.model_list, ["adapter-owned-model"])

    def test_video_uses_vlm_task_when_dedicated_video_task_is_absent(self):
        from src.multimodal.remote import RemotePerceptionProvider

        task = SimpleNamespace(model_list=["remote"])
        remote = SimpleNamespace(name="Remote", base_url="https://example.invalid/v1")
        model = SimpleNamespace(name="remote", api_provider="Remote")
        model_config = SimpleNamespace(
            api_providers=[remote],
            models=[model],
            model_task_config=SimpleNamespace(vlm=task),
        )
        model_config.get_model_info = lambda name: model

        class FakeVideoLLM:
            async def generate_response_for_video(self, *args, **kwargs):
                return "remote video", ()

        provider = RemotePerceptionProvider(model_config=model_config, request_factory=lambda **kwargs: FakeVideoLLM())

        result = asyncio.run(provider.perceive(MediaInput("video.understand.v1", "YQ==", media_format="mp4")))

        self.assertEqual(result.text, "remote video")


class RuntimeProfileTests(unittest.TestCase):
    def test_explicit_profiles_are_selected_independently_of_tts_environment(self):
        with patch.dict(
            os.environ,
            {"NACHOBOT_RUNTIME_PROFILE": "lite", "NACHOBOT_TTS_RUNTIME_PROFILE": "gpu"},
            clear=False,
        ):
            self.assertIs(get_runtime_profile(), RuntimeProfile.LITE)
        with patch.dict(
            os.environ,
            {"NACHOBOT_RUNTIME_PROFILE": "potato", "NACHOBOT_TTS_RUNTIME_PROFILE": "cpu"},
            clear=False,
        ):
            self.assertIs(get_runtime_profile(), RuntimeProfile.POTATO)

    def test_legacy_tts_environment_alone_keeps_safe_full_default(self):
        with patch.dict(
            os.environ,
            {"NACHOBOT_RUNTIME_PROFILE": "", "NACHOBOT_TTS_RUNTIME_PROFILE": "cpu"},
            clear=False,
        ):
            self.assertIs(get_runtime_profile(), RuntimeProfile.FULL)

    def test_tts_profile_names_are_invalid_product_profiles(self):
        for value in ("gpu", "cpu", "relay", "potato_relay", "unexpected"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"NACHOBOT_RUNTIME_PROFILE": value, "NACHOBOT_TTS_RUNTIME_PROFILE": "gpu"},
                clear=False,
            ):
                self.assertIs(get_runtime_profile(), RuntimeProfile.FULL)


class MediaBoundTests(unittest.TestCase):
    def test_oversized_encoded_audio_is_rejected_before_decode(self):
        oversized = "A" * (max_base64_chars(MAX_AUDIO_BYTES) + 1)

        with patch("src.multimodal.contracts.base64.b64decode") as decoder:
            with self.assertRaisesRegex(ValueError, "exceeds"):
                decode_bounded_base64(oversized, max_bytes=MAX_AUDIO_BYTES)

        decoder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
