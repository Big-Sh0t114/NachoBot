from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.chat.utils.utils_video import VideoManager
from src.multimodal.contracts import MAX_VIDEO_BYTES


def _manager_without_initialization() -> VideoManager:
    # Avoid VideoManager.__init__, which creates directories and opens the
    # application database. These tests exercise the real async methods.
    return object.__new__(VideoManager)


class VideoSegmentPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_payload_is_passed_to_existing_video_facade(self):
        raw_video = b"mp4-video-bytes"
        encoded = base64.b64encode(raw_video).decode("ascii")
        manager = _manager_without_initialization()

        class FakeVideoRouter:
            def __init__(self):
                self.calls = []

            async def understand_video(self, data, **kwargs):
                self.calls.append((data, kwargs))
                return SimpleNamespace(text="视频描述", degraded=False)

        router = FakeVideoRouter()
        with tempfile.TemporaryDirectory() as temp_dir:
            manager.VIDEO_DIR = temp_dir
            (Path(temp_dir) / "video").mkdir()
            with (
                patch("src.multimodal.get_multimodal_router", return_value=router),
                patch("src.chat.utils.utils_video.ImageDescriptions.get_or_none", return_value=None),
                patch("src.chat.utils.utils_video.ImageDescriptions.create"),
                patch("httpx.AsyncClient", side_effect=AssertionError("inline payload must not download its URL")),
            ):
                result = await manager.process_video(
                    {
                        "base64": encoded,
                        "size": len(raw_video),
                        "name": "clip.mp4",
                        "url": "https://example.invalid/clip.mp4",
                    }
                )

        self.assertEqual(result, "[视频：视频描述]")
        self.assertEqual(len(router.calls), 1)
        self.assertEqual(router.calls[0][0], encoded)
        self.assertEqual(router.calls[0][1]["media_format"], "mp4")
        self.assertEqual(router.calls[0][1]["metadata"]["temperature"], 0.4)

    async def test_invalid_and_oversized_inline_payloads_are_rejected_before_decode(self):
        invalid_payloads = (
            {"base64": b"YQ==", "size": 1},
            {"base64": "YQ==", "size": MAX_VIDEO_BYTES + 1},
            {"base64": "YQ==", "size": True},
        )

        with patch("src.multimodal.contracts.base64.b64decode") as decoder:
            for payload in invalid_payloads:
                with self.subTest(payload_keys=tuple(payload)):
                    self.assertIsNone(await _manager_without_initialization()._download_or_read_video(payload))
            # Exercise the encoded-length guard without allocating an 85 MiB
            # test string; the actual production limit comes from contracts.
            with patch("src.chat.utils.utils_video.max_base64_chars", return_value=3):
                self.assertIsNone(
                    await _manager_without_initialization()._download_or_read_video(
                        {"base64": "YQ==", "size": 1}
                    )
                )

        decoder.assert_not_called()

    async def test_declared_size_must_match_decoded_payload(self):
        result = await _manager_without_initialization()._download_or_read_video(
            {"base64": "YWJj", "size": 2}
        )

        self.assertIsNone(result)

    async def test_existing_path_and_url_inputs_remain_supported(self):
        manager = _manager_without_initialization()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "clip.mp4"
            path.write_bytes(b"local-video")
            self.assertEqual(
                await manager._download_or_read_video({"path": str(path)}),
                b"local-video",
            )

        class FakeResponse:
            status_code = 200
            content = b"remote-video"

        class FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, *, timeout):
                self.url = url
                self.timeout = timeout
                return FakeResponse()

        fake_client = FakeAsyncClient()
        with patch("httpx.AsyncClient", return_value=fake_client):
            self.assertEqual(
                await manager._download_or_read_video({"url": "https://example.invalid/clip.mp4"}),
                b"remote-video",
            )
        self.assertEqual(fake_client.url, "https://example.invalid/clip.mp4")
        self.assertEqual(fake_client.timeout, 60)


if __name__ == "__main__":
    unittest.main()
