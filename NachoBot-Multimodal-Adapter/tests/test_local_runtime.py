from __future__ import annotations

import base64
import os
import unittest
from unittest.mock import patch

from nachobot_multimodal.local_runtime import LocalMultimodalRuntime, UnsupportedOperation

class LocalRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_is_perception_only(self):
        from nachobot_multimodal import api_server

        paths = {route.path for route in api_server.app.routes}
        self.assertIn("/v1/perception", paths)
        self.assertIn("/v1/audio/transcriptions", paths)
        self.assertFalse(any(path.startswith("/v1/tts") for path in paths))

    async def test_api_lifespan_propagates_preload_failure(self):
        from nachobot_multimodal import api_server

        class FailingRuntime:
            perception_enabled = True

            async def preload(self):
                raise RuntimeError("preload failed")

        with patch.object(api_server, "_runtime", FailingRuntime()):
            with self.assertRaisesRegex(RuntimeError, "preload failed"):
                async with api_server.lifespan(api_server.app):
                    self.fail("a failed preload must not enter the ready lifespan")

    async def test_capabilities_are_perception_only_and_video_is_explicitly_unsupported(self):
        runtime = LocalMultimodalRuntime(
            no_local_models=False,
            asr_transcriber=lambda data: "recognized",
            image_captioner=lambda data: "caption",
        )
        await runtime.preload()

        capabilities = runtime.capabilities().to_dict()

        self.assertIn("audio.transcribe.v1", capabilities["operations"])
        self.assertIn("image.describe.v1", capabilities["operations"])
        self.assertNotIn("video.understand.v1", capabilities["operations"])
        self.assertNotIn("tts", capabilities)
        self.assertTrue(capabilities["ready"])
        self.assertTrue(capabilities["models_loaded"])
        with self.assertRaises(UnsupportedOperation):
            await runtime.perceive("video.understand.v1", base64.b64encode(b"v").decode())

    async def test_local_operation_routes_to_injected_models_without_weights(self):
        runtime = LocalMultimodalRuntime(
            no_local_models=False,
            asr_transcriber=lambda data: "recognized",
            image_captioner=lambda data: "caption",
        )
        await runtime.preload()
        encoded = base64.b64encode(b"media").decode()

        self.assertEqual(await runtime.perceive("audio.transcribe.v1", encoded), "recognized")
        self.assertEqual(await runtime.perceive("image.describe.v1", encoded), "caption")

    async def test_disabled_runtime_rejects_operations(self):
        runtime = LocalMultimodalRuntime(no_local_models=True)

        capabilities = runtime.capabilities().to_dict()
        self.assertFalse(capabilities["ready"])
        self.assertFalse(capabilities["models_loaded"])
        self.assertNotIn("tts", capabilities)
        with self.assertRaises(UnsupportedOperation):
            await runtime.perceive("audio.transcribe.v1", base64.b64encode(b"media").decode())

    async def test_disable_vlm_asr_keeps_perception_listener_not_ready(self):
        with patch.dict(
            os.environ,
            {"DISABLE_VLM_ASR": "1", "NACHOBOT_NO_LOCAL_MODELS": "0"},
            clear=False,
        ):
            runtime = LocalMultimodalRuntime(
                asr_transcriber=lambda data: "recognized",
                image_captioner=lambda data: "caption",
            )

            capabilities = runtime.capabilities().to_dict()
            self.assertEqual(capabilities["operations"], [])
            self.assertFalse(capabilities["perception_enabled"])
            self.assertTrue(capabilities["perception_disabled"])
            self.assertFalse(capabilities["no_local_models"])
            self.assertFalse(capabilities["ready"])
            with self.assertRaises(UnsupportedOperation):
                await runtime.perceive("audio.transcribe.v1", base64.b64encode(b"media").decode())

    async def test_no_local_models_disables_perception(self):
        with patch.dict(
            os.environ,
            {"NACHOBOT_NO_LOCAL_MODELS": "1", "DISABLE_VLM_ASR": "1"},
            clear=False,
        ):
            runtime = LocalMultimodalRuntime()

            capabilities = runtime.capabilities().to_dict()
            self.assertTrue(capabilities["no_local_models"])
            self.assertFalse(capabilities["perception_enabled"])
            self.assertFalse(capabilities["ready"])
            with self.assertRaises(UnsupportedOperation):
                await runtime.perceive("audio.transcribe.v1", base64.b64encode(b"media").decode())

    async def test_preload_calls_both_model_loaders_before_ready(self):
        calls: list[str] = []

        def asr_loader():
            calls.append("asr")

        def vlm_loader():
            calls.append("vlm")

        runtime = LocalMultimodalRuntime(
            asr_transcriber=lambda data: "recognized",
            image_captioner=lambda data: "caption",
            asr_loader=asr_loader,
            vlm_loader=vlm_loader,
        )

        self.assertFalse(runtime.capabilities().to_dict()["ready"])
        await runtime.preload()
        capabilities = runtime.capabilities().to_dict()
        self.assertEqual(calls, ["asr", "vlm"])
        self.assertTrue(capabilities["ready"])

    async def test_preload_failure_is_latched_and_never_retried_by_request(self):
        calls: list[str] = []

        def failing_loader():
            calls.append("asr")
            raise RuntimeError("weights missing")

        runtime = LocalMultimodalRuntime(
            asr_loader=failing_loader,
            image_captioner=lambda data: "caption",
        )

        with self.assertRaisesRegex(RuntimeError, "preload"):
            await runtime.preload()
        with self.assertRaises(UnsupportedOperation):
            await runtime.perceive("audio.transcribe.v1", base64.b64encode(b"media").decode())
        with self.assertRaisesRegex(RuntimeError, "preload"):
            await runtime.preload()
        self.assertEqual(calls, ["asr"])
        self.assertFalse((await runtime.health())["ready"])

    async def test_oversized_encoded_media_is_rejected_before_decode(self):
        runtime = LocalMultimodalRuntime(
            no_local_models=False,
            asr_transcriber=lambda data: "should-not-run",
            image_captioner=lambda data: "should-not-run",
        )
        await runtime.preload()
        limit = 16 * 1024 * 1024
        oversized = "A" * (4 * ((limit + 2) // 3) + 1)

        with patch("nachobot_multimodal.local_runtime.base64.b64decode") as decoder:
            with self.assertRaisesRegex(ValueError, "exceeds"):
                await runtime.perceive("audio.transcribe.v1", oversized)

        decoder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
