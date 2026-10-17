"""Focused tests for UniversalVC support-model downloads."""

import hashlib
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

import model_manager as model_manager_module
from model_manager import ModelManager


class FakeResponse:
    def __init__(self, chunks, *, content_length=None, status_code=200):
        self.chunks = chunks
        self.status_code = status_code
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class ModelManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.manager = ModelManager(
            self.temp_dir.name,
            logger=logging.getLogger("test.model_manager"),
        )

    def test_hf_endpoints_follow_priority_and_deduplicate_normalized_values(self):
        environment = {
            "NACHOBOT_HF_ENDPOINT": "  https://mirror.example/custom///  ",
            "HF_ENDPOINT": "https://mirror.example/custom/",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                ModelManager._hf_endpoints(),
                [
                    "https://mirror.example/custom",
                    "https://hf-mirror.com",
                    "https://huggingface.co",
                ],
            )

    def test_blank_hf_endpoint_variables_are_ignored(self):
        environment = {
            "NACHOBOT_HF_ENDPOINT": "  ",
            "HF_ENDPOINT": "\t",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                ModelManager._hf_endpoints(),
                ["https://hf-mirror.com", "https://huggingface.co"],
            )

    def test_each_model_uses_its_hf_source_and_existing_local_filename(self):
        cases = {
            "silero_vad": (
                "codeandwhisky/sva-genesis",
                "silero_vad.onnx",
                "silero_vad.onnx",
            ),
            "wespeaker": (
                "csukuangfj/speaker-embedding-models",
                "wespeaker_zh_cnceleb_resnet34.onnx",
                "wespeaker_resnet34.onnx",
            ),
        }
        environment = {"NACHOBOT_HF_ENDPOINT": "https://custom.example/"}
        with patch.dict(os.environ, environment, clear=True):
            for model_key, (repo, hf_filename, local_filename) in cases.items():
                with self.subTest(model_key=model_key):
                    attempted = []

                    def succeed(url, target, expected_sha256=None):
                        attempted.append((url, target.name, expected_sha256))
                        return True

                    with patch.object(self.manager, "_download", side_effect=succeed):
                        self.assertTrue(self.manager._ensure_file(model_key))

                    info = model_manager_module._MODELS[model_key]
                    self.assertEqual(info["filename"], local_filename)
                    self.assertEqual(
                        attempted,
                        [
                            (
                                "https://custom.example/"
                                f"{repo}/resolve/main/{hf_filename}?download=true",
                                local_filename,
                                info["sha256"],
                            )
                        ],
                    )

    def test_cached_file_skips_all_download_attempts(self):
        target = Path(self.manager.get_model_paths()["silero_vad"])
        target.write_bytes(b"existing local model")

        with patch.object(model_manager_module.requests, "get") as get:
            self.assertTrue(self.manager._ensure_file("silero_vad"))

        get.assert_not_called()
        self.assertEqual(target.read_bytes(), b"existing local model")

    def test_failed_endpoint_falls_back_to_later_endpoint_and_replaces_atomically(self):
        body = b"valid model bytes"
        expected_hash = hashlib.sha256(body).hexdigest()
        self._set_silero_hash(expected_hash)
        failed = FakeResponse([], status_code=503)
        succeeded = FakeResponse([body], content_length=len(body))
        urls = []
        target = Path(self.manager.get_model_paths()["silero_vad"])
        partial = target.with_name(f"{target.name}.part")

        def get(url, **kwargs):
            urls.append(url)
            self.assertTrue(kwargs["stream"])
            self.assertEqual(kwargs["timeout"], 300)
            if len(urls) == 2:
                self.assertFalse(partial.exists())
            return (failed, succeeded)[len(urls) - 1]

        environment = {
            "NACHOBOT_HF_ENDPOINT": "https://first.example",
            "HF_ENDPOINT": "https://second.example",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch.object(model_manager_module.requests, "get", side_effect=get):
                self.assertTrue(self.manager._ensure_file("silero_vad"))

        self.assertEqual(
            urls,
            [
                "https://first.example/codeandwhisky/sva-genesis/resolve/main/"
                "silero_vad.onnx?download=true",
                "https://second.example/codeandwhisky/sva-genesis/resolve/main/"
                "silero_vad.onnx?download=true",
            ],
        )
        self.assertTrue(failed.closed)
        self.assertTrue(succeeded.closed)
        self.assertEqual(target.read_bytes(), body)
        self.assertFalse(partial.exists())

    def test_stream_failure_removes_partial_then_later_endpoint_succeeds(self):
        body = b"complete bytes"
        self._set_silero_hash(hashlib.sha256(body).hexdigest())
        broken = FakeResponse(
            [b"partial", OSError("connection reset")],
            content_length=len(body),
        )
        succeeded = FakeResponse([body], content_length=len(body))
        target = Path(self.manager.get_model_paths()["silero_vad"])
        partial = target.with_name(f"{target.name}.part")
        urls = []

        def get(url, **kwargs):
            urls.append(url)
            if len(urls) == 2:
                self.assertFalse(partial.exists())
            return (broken, succeeded)[len(urls) - 1]

        with patch.dict(os.environ, {"NACHOBOT_HF_ENDPOINT": "https://one.example"}, clear=True):
            with patch.object(model_manager_module.requests, "get", side_effect=get):
                self.assertTrue(self.manager._ensure_file("silero_vad"))

        self.assertEqual(len(urls), 2)
        self.assertTrue(broken.closed)
        self.assertTrue(succeeded.closed)
        self.assertEqual(target.read_bytes(), body)
        self.assertFalse(partial.exists())

    def test_checksum_mismatch_is_removed_before_fallback(self):
        body = b"the expected model bytes"
        self._set_silero_hash(hashlib.sha256(body).hexdigest())
        mismatched = FakeResponse([b"wrong model bytes"], content_length=len(b"wrong model bytes"))
        succeeded = FakeResponse([body], content_length=len(body))
        target = Path(self.manager.get_model_paths()["silero_vad"])
        partial = target.with_name(f"{target.name}.part")
        urls = []

        def get(url, **kwargs):
            urls.append(url)
            if len(urls) == 2:
                self.assertFalse(partial.exists())
            return (mismatched, succeeded)[len(urls) - 1]

        with patch.dict(os.environ, {"NACHOBOT_HF_ENDPOINT": "https://one.example"}, clear=True):
            with patch.object(model_manager_module.requests, "get", side_effect=get):
                self.assertTrue(self.manager._ensure_file("silero_vad"))

        self.assertEqual(len(urls), 2)
        self.assertTrue(mismatched.closed)
        self.assertTrue(succeeded.closed)
        self.assertEqual(target.read_bytes(), body)
        self.assertFalse(partial.exists())

    def test_all_hf_endpoints_fail_before_original_github_url(self):
        body = b"github fallback model bytes"
        self._set_silero_hash(hashlib.sha256(body).hexdigest())
        responses = [
            FakeResponse([], status_code=503),
            FakeResponse([], status_code=503),
            FakeResponse([body], content_length=len(body)),
        ]
        urls = []

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(
                model_manager_module.requests,
                "get",
                side_effect=lambda url, **kwargs: urls.append(url) or responses[len(urls) - 1],
            ):
                self.assertTrue(self.manager._ensure_file("silero_vad"))

        self.assertEqual(
            urls,
            [
                "https://hf-mirror.com/codeandwhisky/sva-genesis/resolve/main/"
                "silero_vad.onnx?download=true",
                "https://huggingface.co/codeandwhisky/sva-genesis/resolve/main/"
                "silero_vad.onnx?download=true",
                model_manager_module._MODELS["silero_vad"]["url"],
            ],
        )
        self.assertTrue(all(response.closed for response in responses))
        self.assertEqual(
            Path(self.manager.get_model_paths()["silero_vad"]).read_bytes(),
            body,
        )

    def test_all_endpoints_fail_returns_false(self):
        self._set_silero_hash(hashlib.sha256(b"ignored").hexdigest())
        responses = [FakeResponse([], status_code=503) for _ in range(5)]
        urls = []
        environment = {
            "NACHOBOT_HF_ENDPOINT": "https://first.example",
            "HF_ENDPOINT": "https://second.example",
        }

        with patch.dict(os.environ, environment, clear=True):
            with patch.object(
                model_manager_module.requests,
                "get",
                side_effect=lambda url, **kwargs: urls.append(url) or responses[len(urls) - 1],
            ):
                self.assertFalse(self.manager._ensure_file("silero_vad"))

        self.assertEqual(len(urls), 5)
        self.assertEqual(urls[-1], model_manager_module._MODELS["silero_vad"]["url"])
        self.assertTrue(all(response.closed for response in responses))
        target = Path(self.manager.get_model_paths()["silero_vad"])
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name(f"{target.name}.part").exists())

    def test_ensure_all_attempts_both_models_after_failure(self):
        with patch.object(self.manager, "_ensure_file", side_effect=[False, True]) as ensure:
            self.assertFalse(self.manager.ensure_all())

        self.assertEqual(
            [call.args[0] for call in ensure.call_args_list],
            ["silero_vad", "wespeaker"],
        )

    def test_empty_and_truncated_responses_are_rejected_and_closed(self):
        cases = (
            ("empty", FakeResponse([], content_length=0)),
            ("truncated", FakeResponse([b"short"], content_length=10)),
        )
        expected_hash = hashlib.sha256(b"expected model bytes").hexdigest()

        for name, response in cases:
            with self.subTest(name=name):
                target = Path(self.temp_dir.name) / f"{name}.onnx"
                with patch.object(
                    model_manager_module.requests,
                    "get",
                    return_value=response,
                ):
                    self.assertFalse(
                        self.manager._download(
                            f"https://example.test/{name}",
                            target,
                            expected_hash,
                        )
                    )

                self.assertTrue(response.closed)
                self.assertFalse(target.exists())
                self.assertFalse(target.with_name(f"{target.name}.part").exists())

    def _set_silero_hash(self, expected_hash):
        info = dict(model_manager_module._MODELS["silero_vad"])
        info["sha256"] = expected_hash
        self.enterContext(
            patch.dict(model_manager_module._MODELS, {"silero_vad": info})
        )


if __name__ == "__main__":
    unittest.main()
