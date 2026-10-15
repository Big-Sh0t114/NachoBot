from __future__ import annotations

import asyncio
import os
import base64
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

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
from src.multimodal.client import LocalMultimodalClient, LocalPerceptionError
from src.multimodal.profile import RuntimeProfile, get_runtime_profile
from src.multimodal.router import AudioStreamError, CoreMultimodalRouter, _ASRReceipt
from src.chat.message_receive.message import MessageProcessBase, MessageRecv
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
        return {
            "ready": True,
            "operations": [AUDIO_TRANSCRIBE_V1, IMAGE_DESCRIBE_V1],
            "models": {AUDIO_TRANSCRIBE_V1: "zh-xlarge-int8-2025-06-30", IMAGE_DESCRIBE_V1: "Florence-2"},
        }

    async def tts_health(self):
        return {"status": "ok", "ready": True, "model_loaded": True}


class FakeStreamingPerception(FakePerception):
    def __init__(self, *, loaded_model="asr-b", finish_text="finished transcript", streaming_asr=True):
        super().__init__()
        self.loaded_model = loaded_model
        self.finish_text = finish_text
        self.streaming_asr = streaming_asr
        self.stream_starts: list[str] = []
        self.stream_chunks: list[tuple[str, int, bytes]] = []
        self.stream_finishes: list[str] = []
        self.stream_aborts: list[str] = []
        self.closed = False

    async def health(self):
        return {
            "ready": True,
            "operations": [AUDIO_TRANSCRIBE_V1, IMAGE_DESCRIBE_V1],
            "models": {AUDIO_TRANSCRIBE_V1: self.loaded_model, IMAGE_DESCRIBE_V1: "Florence-2"},
            "streaming_asr": self.streaming_asr,
        }

    async def start_audio_stream(self, model_identifier):
        self.stream_starts.append(model_identifier)
        return "runtime-stream-1"

    async def append_audio_stream_chunk(self, stream_id, seq, pcm):
        self.stream_chunks.append((stream_id, seq, pcm))
        return f"partial-{seq}"

    async def finish_audio_stream(self, stream_id):
        self.stream_finishes.append(stream_id)
        return {"text": self.finish_text, "result_id": "runtime-result-id"}

    async def abort_audio_stream(self, stream_id):
        self.stream_aborts.append(stream_id)

    async def aclose(self):
        self.closed = True


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


class _QueuedJsonHttpClient(_RecordingHttpClient):
    def __init__(self, payloads):
        super().__init__()
        self.payloads = list(payloads)
        self.closed = False

    async def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return _JsonResponse(self.payloads.pop(0))

    async def aclose(self):
        self.closed = True


def _mixed_config(group: str):
    task = SimpleNamespace(model_list=["remote-first", "local-middle", "remote-last"], max_tokens=20, temperature=0.3, timeout=None)
    providers = {
        "remote": SimpleNamespace(name="Remote", base_url="https://example.invalid/v1", max_retry=1),
        "local": SimpleNamespace(name="LocalModel", base_url="http://127.0.0.1:9874/v1", max_retry=1),
    }
    models = {
        name: SimpleNamespace(
            name=name,
            api_provider="local" if name == "local-middle" else "remote",
            model_identifier=("zh-xlarge-int8-2025-06-30" if group == "voice" else "Florence-2")
            if name == "local-middle" else name,
        )
        for name in task.model_list
    }
    config = SimpleNamespace(model_task_config=SimpleNamespace(**{group: task}))
    config.get_model_info = models.__getitem__
    config.get_provider = providers.__getitem__
    return config


def _stream_config():
    task = SimpleNamespace(
        model_list=["remote-first", "local-a", "local-b"],
        max_tokens=20,
        temperature=0.3,
        timeout=None,
    )
    providers = {
        "remote": SimpleNamespace(name="Remote", base_url="https://example.invalid/v1", max_retry=1),
        "local": SimpleNamespace(name="LocalModel", base_url="http://127.0.0.1:9874/v1", max_retry=1),
    }
    models = {
        "remote-first": SimpleNamespace(name="remote-first", api_provider="remote", model_identifier="remote-asr"),
        "local-a": SimpleNamespace(name="local-a", api_provider="local", model_identifier="asr-a"),
        "local-b": SimpleNamespace(name="local-b", api_provider="local", model_identifier="asr-b"),
    }
    config = SimpleNamespace(model_task_config=SimpleNamespace(voice=task))
    config.get_model_info = models.__getitem__
    config.get_provider = providers.__getitem__
    return config


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

    async def test_full_uses_one_mixed_model_group_for_remote_local_remote(self):
        from src.llm_models.exceptions import ModelAttemptFailed
        from src.llm_models.model_client.base_client import APIResponse, client_registry
        from src.llm_models.utils_model import LLMRequest

        config = _mixed_config("voice")
        local = FakePerception(error=RuntimeError("busy"))
        remote_calls = []

        async def remote_attempt(_llm, model, *_args, **_kwargs):
            remote_calls.append(model.name)
            if model.name == "remote-first":
                raise ModelAttemptFailed("remote failed")
            return APIResponse(content="remote last")

        with patch.object(client_registry, "get_client_class_instance", return_value=object()), patch.object(
            LLMRequest, "_attempt_request_on_model", remote_attempt
        ):
            result = await CoreMultimodalRouter(profile="full", local=local, model_config=config).transcribe("YQ==")

        self.assertEqual(result.text, "remote last")
        self.assertEqual(result.attempted, ("remote-first", "local-middle", "remote-last"))
        self.assertEqual(local.calls, [AUDIO_TRANSCRIBE_V1])
        self.assertEqual(remote_calls, ["remote-first", "remote-last"])

    async def test_lite_skips_local_candidate_in_same_model_group(self):
        from src.llm_models.exceptions import ModelAttemptFailed
        from src.llm_models.model_client.base_client import APIResponse, client_registry
        from src.llm_models.utils_model import LLMRequest

        config = _mixed_config("voice")
        local = FakePerception()

        async def remote_attempt(_llm, model, *_args, **_kwargs):
            if model.name == "remote-first":
                raise ModelAttemptFailed("remote failed")
            return APIResponse(content="remote last")

        with patch.object(client_registry, "get_client_class_instance", return_value=object()), patch.object(
            LLMRequest, "_attempt_request_on_model", remote_attempt
        ):
            result = await CoreMultimodalRouter(profile="lite", local=local, model_config=config).transcribe("YQ==")

        self.assertEqual(result.text, "remote last")
        self.assertEqual(result.attempted, ("remote-first", "local-middle", "remote-last"))
        self.assertEqual(local.calls, [])

    async def test_video_capability_skips_local_before_post(self):
        from src.llm_models.model_client.base_client import APIResponse, client_registry
        from src.llm_models.utils_model import LLMRequest

        config = _mixed_config("video")
        config.model_task_config.video.model_list = ["local-middle", "remote-last"]
        local = FakePerception()

        async def remote_attempt(_llm, _model, *_args, **_kwargs):
            return APIResponse(content="remote video")

        with patch.object(client_registry, "get_client_class_instance", return_value=object()), patch.object(
            LLMRequest, "_attempt_request_on_model", remote_attempt
        ):
            result = await CoreMultimodalRouter(profile="full", local=local, model_config=config).understand_video(
                "YQ==", media_format="mp4"
            )

        self.assertEqual(result.text, "remote video")
        self.assertEqual(result.attempted, ("local-middle", "remote-last"))
        self.assertEqual(local.calls, [])

    async def test_stream_selects_once_orders_chunks_and_consumes_receipt_once(self):
        from src.llm_models.model_client.base_client import client_registry

        config = _stream_config()
        local = FakeStreamingPerception()
        router = CoreMultimodalRouter(profile="full", local=local, model_config=config)
        self.addAsyncCleanup(router.shutdown)
        _, request = router._request_for_task("voice")
        request.model_usage["local-a"] = (0, 0, 4, 0.0)
        request.model_usage["local-b"] = (0, 0, 0, 0.0)

        with patch.object(client_registry, "get_client_class_instance", return_value=object()):
            started = await router.start_audio_stream()

        stream_id = started["stream_id"]
        session = router._audio_streams[stream_id]
        self.assertEqual(session.platform, "universal_vc")
        self.assertEqual(session.model_name, "local-b")
        self.assertEqual(session.model_identifier, "asr-b")
        self.assertEqual(local.stream_starts, ["asr-b"])
        self.assertEqual(request.model_usage["local-b"][2], 1)

        pcm = b"\x00\x01\x02\x03"
        encoded = base64.b64encode(pcm).decode("ascii")
        first = await router.append_audio_stream_chunk(stream_id=stream_id, seq=0, pcm_base64=encoded)
        duplicate = await router.append_audio_stream_chunk(stream_id=stream_id, seq=0, pcm_base64=encoded)
        self.assertEqual(first, {"seq": 0, "partial_text": "partial-0"})
        self.assertEqual(duplicate, first)
        self.assertEqual(len(local.stream_chunks), 1)

        with self.assertRaises(AudioStreamError) as gap:
            await router.append_audio_stream_chunk(stream_id=stream_id, seq=2, pcm_base64=encoded)
        self.assertEqual(gap.exception.status_code, 409)
        with self.assertRaises(AudioStreamError) as changed_duplicate:
            await router.append_audio_stream_chunk(
                stream_id=stream_id,
                seq=0,
                pcm_base64=base64.b64encode(b"\x09\x09").decode("ascii"),
            )
        self.assertEqual(changed_duplicate.exception.status_code, 409)

        finished = await router.finish_audio_stream(stream_id)
        self.assertEqual(finished["text"], "finished transcript")
        self.assertNotEqual(finished["result_id"], "runtime-result-id")
        self.assertEqual(request.model_usage["local-b"][2], 0)
        self.assertEqual(local.stream_finishes, ["runtime-stream-1"])

        wrong_context = await router.consume_precomputed_asr_result(
            finished["result_id"], context="discord_vc"
        )
        self.assertIsNone(wrong_context)
        result = await router.transcribe(
            "YQ==",
            precomputed_asr_result_id=finished["result_id"],
            precomputed_asr_context="UniversalVC",
        )
        self.assertEqual(result.text, "finished transcript")
        self.assertTrue(result.metadata["precomputed"])
        self.assertEqual(local.calls, [])
        self.assertIsNone(
            await router.consume_precomputed_asr_result(
                finished["result_id"], context="UniversalVC"
            )
        )

    async def test_stream_receipt_is_bound_to_start_platform(self):
        from src.llm_models.model_client.base_client import client_registry

        router = CoreMultimodalRouter(
            profile="full",
            local=FakeStreamingPerception(loaded_model="asr-a"),
            model_config=_stream_config(),
        )
        self.addAsyncCleanup(router.shutdown)
        with patch.object(client_registry, "get_client_class_instance", return_value=object()):
            with self.assertRaises(AudioStreamError) as invalid:
                await router.start_audio_stream(platform="bilibili.live")
            self.assertEqual(invalid.exception.status_code, 400)
            started = await router.start_audio_stream(platform="DiscordVC")

        session = router._audio_streams[started["stream_id"]]
        self.assertEqual(session.platform, "discord_vc")
        finished = await router.finish_audio_stream(started["stream_id"])
        self.assertEqual(router._asr_receipts[finished["result_id"]].context, "discord_vc")

        wav_calls = []

        async def ordinary_asr(request):
            wav_calls.append(request)
            return PerceptionResult(request.operation, "ordinary transcript", provider="remote")

        router.perceive = ordinary_asr
        mismatch = await router.transcribe(
            "YQ==",
            precomputed_asr_result_id=finished["result_id"],
            precomputed_asr_context="universal_vc",
        )
        self.assertEqual(mismatch.text, "ordinary transcript")
        self.assertEqual(len(wav_calls), 1)

        matched = await router.transcribe(
            "YQ==",
            precomputed_asr_result_id=finished["result_id"],
            precomputed_asr_context="discord_vc",
        )
        self.assertEqual(matched.text, "finished transcript")
        self.assertTrue(matched.metadata["precomputed"])
        self.assertEqual(len(wav_calls), 1)

    async def test_stream_receipt_requires_exact_scope_without_consuming_on_mismatch(self):
        from src.llm_models.model_client.base_client import client_registry

        router = CoreMultimodalRouter(
            profile="full",
            local=FakeStreamingPerception(loaded_model="asr-a"),
            model_config=_stream_config(),
        )
        self.addAsyncCleanup(router.shutdown)
        scope = "user:logical-user-8|channel:voice-channel-4|generation:22|capture:51"
        with patch.object(client_registry, "get_client_class_instance", return_value=object()):
            started = await router.start_audio_stream(platform="discord", scope=scope)
        session = router._audio_streams[started["stream_id"]]
        self.assertEqual(session.scope, scope)

        finished = await router.finish_audio_stream(started["stream_id"])
        receipt = router._asr_receipts[finished["result_id"]]
        self.assertEqual(receipt.context, "discord")
        self.assertEqual(receipt.scope, scope)

        wav_calls = []

        async def ordinary_asr(request):
            wav_calls.append(request)
            return PerceptionResult(request.operation, "wav fallback", provider="remote")

        router.perceive = ordinary_asr
        for wrong_scope in (
            scope.replace("logical-user-8", "logical-user-9"),
            scope.replace("voice-channel-4", "voice-channel-5"),
            scope.replace("generation:22", "generation:23"),
        ):
            result = await router.transcribe(
                "YQ==",
                precomputed_asr_result_id=finished["result_id"],
                precomputed_asr_context="discord",
                precomputed_asr_scope=wrong_scope,
            )
            self.assertEqual(result.text, "wav fallback")
            self.assertIn(finished["result_id"], router._asr_receipts)

        matched = await router.transcribe(
            "YQ==",
            precomputed_asr_result_id=finished["result_id"],
            precomputed_asr_context="discord",
            precomputed_asr_scope=scope,
        )
        self.assertEqual(matched.text, "finished transcript")
        self.assertTrue(matched.metadata["precomputed"])
        self.assertEqual(len(wav_calls), 3)
        replayed = await router.transcribe(
            "YQ==",
            precomputed_asr_result_id=finished["result_id"],
            precomputed_asr_context="discord",
            precomputed_asr_scope=scope,
        )
        self.assertEqual(replayed.text, "wav fallback")
        self.assertEqual(len(wav_calls), 4)

    async def test_malformed_receipt_scope_uses_wav_fallback(self):
        router = CoreMultimodalRouter(profile="full", local=FakePerception())
        router._asr_receipts["scoped"] = _ASRReceipt("stream text", "discord", time.monotonic() + 30, "scope")
        calls = []

        async def full_wav(data, **kwargs):
            calls.append((data, kwargs))
            return PerceptionResult(AUDIO_TRANSCRIBE_V1, "wav fallback", provider="remote")

        router.perceive = full_wav
        result = await router.transcribe(
            "YQ==",
            precomputed_asr_result_id="scoped",
            precomputed_asr_context="discord",
            precomputed_asr_scope=["malformed"],
        )

        self.assertEqual(result.text, "wav fallback")
        self.assertEqual(calls, [(MediaInput(operation=AUDIO_TRANSCRIBE_V1, data="YQ=="), {})])
        self.assertIn("scoped", router._asr_receipts)

    async def test_stream_mismatch_fails_without_changing_wav_model_selection(self):
        from src.llm_models.exceptions import ModelAttemptFailed
        from src.llm_models.model_client.base_client import APIResponse, client_registry
        from src.llm_models.utils_model import LLMRequest

        config = _stream_config()
        local = FakeStreamingPerception(loaded_model="unrecognized-model")
        router = CoreMultimodalRouter(profile="full", local=local, model_config=config)
        self.addAsyncCleanup(router.shutdown)

        async def remote_attempt(_llm, model, *_args, **_kwargs):
            if model.name != "remote-first":
                raise ModelAttemptFailed("unexpected candidate")
            return APIResponse(content="ordinary WAV transcript")

        with patch.object(client_registry, "get_client_class_instance", return_value=object()), patch.object(
            LLMRequest, "_attempt_request_on_model", remote_attempt
        ):
            with self.assertRaises(AudioStreamError) as caught:
                await router.start_audio_stream()
            result = await router.transcribe("YQ==")

        self.assertEqual(caught.exception.status_code, 501)
        self.assertEqual(local.stream_starts, [])
        self.assertEqual(result.text, "ordinary WAV transcript")
        self.assertEqual(result.attempted, ("remote-first",))
        self.assertEqual(local.calls, [])

    async def test_stream_expiry_and_shutdown_abort_runtime_and_release_leases(self):
        from src.llm_models.model_client.base_client import client_registry

        config = _stream_config()
        local = FakeStreamingPerception()
        router = CoreMultimodalRouter(profile="full", local=local, model_config=config)
        _, request = router._request_for_task("voice")
        request.model_usage["local-a"] = (0, 0, 4, 0.0)
        request.model_usage["local-b"] = (0, 0, 0, 0.0)
        with patch.object(client_registry, "get_client_class_instance", return_value=object()):
            first = await router.start_audio_stream()
        expired = router._audio_streams[first["stream_id"]]
        expired.last_activity = time.monotonic() - 16
        await router.cleanup_expired_audio_streams()
        self.assertEqual(local.stream_aborts, ["runtime-stream-1"])
        self.assertEqual(request.model_usage["local-b"][2], 0)

        with patch.object(client_registry, "get_client_class_instance", return_value=object()):
            second = await router.start_audio_stream()
        self.assertEqual(request.model_usage["local-b"][2], 1)
        await router.shutdown()
        self.assertEqual(local.stream_aborts, ["runtime-stream-1", "runtime-stream-1"])
        self.assertEqual(request.model_usage["local-b"][2], 0)
        self.assertTrue(local.closed)

    async def test_expired_busy_stream_does_not_block_another_stream(self):
        from src.llm_models.model_client.base_client import client_registry

        local = FakeStreamingPerception()
        router = CoreMultimodalRouter(
            profile="full", local=local, model_config=_stream_config()
        )
        self.addAsyncCleanup(router.shutdown)
        _, request = router._request_for_task("voice")
        request.model_usage["local-a"] = (0, 0, 4, 0.0)
        request.model_usage["local-b"] = (0, 0, 0, 0.0)
        with patch.object(client_registry, "get_client_class_instance", return_value=object()):
            first = await router.start_audio_stream()
            second = await router.start_audio_stream()

        busy = router._audio_streams[first["stream_id"]]
        busy.last_activity = time.monotonic() - 16
        await busy.lock.acquire()
        try:
            await asyncio.wait_for(router.cleanup_expired_audio_streams(), timeout=0.2)
            pcm = base64.b64encode(b"\x00\x01").decode("ascii")
            result = await asyncio.wait_for(
                router.append_audio_stream_chunk(
                    stream_id=second["stream_id"], seq=0, pcm_base64=pcm
                ),
                timeout=0.2,
            )
            self.assertEqual(result["seq"], 0)
            self.assertIn(first["stream_id"], router._audio_streams)
        finally:
            busy.lock.release()

        await router.cleanup_expired_audio_streams()
        self.assertNotIn(first["stream_id"], router._audio_streams)

    async def test_expired_receipt_uses_full_wav_transcription(self):
        router = CoreMultimodalRouter(profile="full", local=FakePerception())
        calls = []

        async def full_wav(data, **kwargs):
            calls.append((data, kwargs))
            return PerceptionResult(AUDIO_TRANSCRIBE_V1, "wav fallback", provider="remote")

        router._asr_receipts["expired"] = _ASRReceipt("stale", "UniversalVC", 0)
        router.perceive = full_wav
        result = await router.transcribe("YQ==", precomputed_asr_result_id="expired")

        self.assertEqual(result.text, "wav fallback")
        self.assertEqual(calls, [(MediaInput(operation=AUDIO_TRANSCRIBE_V1, data="YQ=="), {})])

    async def test_voice_message_forwards_only_allowed_platform_receipts(self):
        receiver = SimpleNamespace(
            message_info=SimpleNamespace(
                platform="UniversalVC",
                additional_config={"precomputed_asr_result_id": "receipt-token"}
            )
        )
        captured = []

        async def get_voice_text(
            data,
            *,
            precomputed_asr_result_id=None,
            precomputed_asr_context=None,
            precomputed_asr_scope=None,
        ):
            captured.append((data, precomputed_asr_result_id, precomputed_asr_context, precomputed_asr_scope))
            return "processed"

        with patch("src.chat.message_receive.message.get_voice_text", get_voice_text):
            for platform in ("UniversalVC", "discord_vc", "bilibili", "webui"):
                receiver.message_info.platform = platform
                result = await MessageRecv._process_single_segment(
                    receiver,
                    Seg(type="voice", data="YQ=="),
                )

            receiver.message_info.platform = "discord"
            receiver.message_info.additional_config["precomputed_asr_scope"] = "opaque-scope"
            await MessageRecv._process_single_segment(receiver, Seg(type="voice", data="YQ=="))
            receiver.message_info.additional_config["precomputed_asr_scope"] = ["malformed"]
            await MessageRecv._process_single_segment(receiver, Seg(type="voice", data="YQ=="))

            receiver.message_info.platform = "qq"
            await MessageRecv._process_single_segment(receiver, Seg(type="voice", data="YQ=="))

        self.assertEqual(result, "processed")
        self.assertEqual(
            captured[:4],
            [
                ("YQ==", "receipt-token", "universal_vc", None),
                ("YQ==", "receipt-token", "discord_vc", None),
                ("YQ==", "receipt-token", "bilibili", None),
                ("YQ==", "receipt-token", "webui", None),
            ],
        )
        self.assertEqual(captured[4], ("YQ==", "receipt-token", "discord", "opaque-scope"))
        self.assertEqual(captured[5], ("YQ==", None, "discord", None))
        self.assertEqual(captured[-1], ("YQ==", None, None, None))

    async def test_local_client_reuses_and_closes_its_owned_http_pool(self):
        transport = _QueuedJsonHttpClient(
            [
                {"ready": True, "operations": [AUDIO_TRANSCRIBE_V1]},
                {"stream_id": "runtime-stream"},
            ]
        )
        with patch("src.multimodal.client.httpx.AsyncClient", return_value=transport) as factory:
            local = LocalMultimodalClient()
            await local.health()
            stream_id = await local.start_audio_stream("asr-b")
            self.assertEqual(stream_id, "runtime-stream")
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(len(transport.calls), 2)
            self.assertEqual(
                transport.calls[1][2]["json"],
                {"sample_rate": 16_000, "channels": 1, "model": "asr-b"},
            )
            await local.aclose()

        self.assertTrue(transport.closed)

        injected = _QueuedJsonHttpClient([])
        injected_client = LocalMultimodalClient(client=injected)
        await injected_client.aclose()
        self.assertFalse(injected.closed)

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

    async def test_local_busy_response_has_failover_category(self):
        class BusyHttp:
            async def request(self, method, url, **kwargs):
                return httpx.Response(
                    503,
                    json={"error": {"code": "busy", "message": "worker busy"}},
                    request=httpx.Request(method, url),
                )

        client = LocalMultimodalClient(client=BusyHttp())
        with self.assertRaises(LocalPerceptionError) as caught:
            await client.perceive(MediaInput(IMAGE_DESCRIBE_V1, "YQ=="))
        self.assertEqual(caught.exception.reason, "busy")

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


class MultimodalTaskTests(unittest.TestCase):
    def test_fast_group_migration_preserves_existing_vlm_models(self):
        from src.config.config import _migrate_renamed_model_task_groups

        target = {"model_task_config": {"vlm_fast": {"model_list": ["template"]}}}
        source = {"model_task_config": {"vlm": {"model_list": ["live-visual"]}}}

        self.assertTrue(_migrate_renamed_model_task_groups(target, source))
        self.assertEqual(target["model_task_config"]["vlm_fast"]["model_list"], ["live-visual"])

    def test_visual_group_contract(self):
        self.assertEqual(MediaInput(IMAGE_DESCRIBE_V1, "YQ==", task="vlm").task, "vlm")
        self.assertEqual(MediaInput(IMAGE_DESCRIBE_V1, "YQ==", task="vlm_fast").task, "vlm_fast")
        with self.assertRaises(ValueError):
            MediaInput(AUDIO_TRANSCRIBE_V1, "YQ==", task="vlm_fast")

    def test_only_9874_is_local_perception_backend(self):
        self.assertTrue(CoreMultimodalRouter._is_local_perception_provider(SimpleNamespace(base_url="http://127.0.0.1:9874/v1")))
        self.assertFalse(CoreMultimodalRouter._is_local_perception_provider(SimpleNamespace(base_url="http://127.0.0.1:11433/v1")))


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
